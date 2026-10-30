"""R3 T1a (#1591): the host's remote session: the Noise responder, the handshake, the open, the per-request checks and
the end of a stream (hub contract 10.2c; design note r3-session-design.md sections 1, 2, 5).

Two layers. The contract vectors are replayed against ``noise.py`` and ``session.py`` on a small fake workspace built
from the vector file's own ids and keys. Behaviour (counters, owners, revocation, timeouts) runs on a real served
workspace with the device side written from ``remote_port`` (the independent reading of the contract)."""

from __future__ import annotations

import contextlib
import json
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orch.canon.jcs import dumps
from orch.crypto.encoding import b64u, unb64u
from orch.host import remote
from orch.host.remote import adapter, counters, gate, noise, session, wrk
from orch.hubclient.errors import HubRefused
from tests.host import human_helpers
from tests.host import remote_port as RP
from tests.host.remote_world import HUB, World, frame, head
from tests.host.test_remote_ops import call, clock_ms, run_on
from tests.host.test_remote_policy import remote_args  # noqa: F401 - used through World

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

CONTRACT = Path(__file__).resolve().parents[4] / "contracts" / "hub"
DOC: dict[str, Any] = json.loads((CONTRACT / "vectors_remote.json").read_text())
X = {n: bytes.fromhex(v["priv_hex"]) for n, v in DOC["keys"]["x25519"].items()}
T = {t["name"]: t for t in DOC["transcripts"]}
IDS = DOC["ids"]
NOW = IDS["now_ms"]
LIST = {d["device_id"]: d for d in DOC["device_list"]["o"]["devices"]}


def cases(check: str) -> list[dict[str, Any]]:
    return [c for c in DOC["cases"] if c["check"] == check]


# --- noise.py against the vectors ------------------------------------------------------------------------------------


def _read_msg1(a: dict[str, Any]) -> noise.Handshake:
    p = a["prologue"]
    pro = noise.prologue(p["kind"], p["workspace_id"], p["id"], p["stream_id"], p["hub_id"])
    return noise.read_msg1(pro, X[a["wrk"]], bytes.fromhex(a["msg1_hex"]))


@pytest.mark.parametrize("c", cases("msg1"), ids=lambda c: c["name"])
def test_noise_vectors_replayed_msg1(c):
    try:
        got: dict[str, Any] = {"ok": {"rs_hex": _read_msg1(c["args"]).rs.hex()}}
    except HubRefused as e:
        got = {"error": e.code}
    assert got == c["expect"]


@pytest.mark.parametrize("c", cases("prologue"), ids=lambda c: c["name"])
def test_noise_vectors_replayed_prologue(c):
    a = c["args"]
    try:
        got: dict[str, Any] = {
            "hex": noise.prologue(a["kind"], a["workspace_id"], a["id"], a["stream_id"], a["hub_id"]).hex()
        }
    except HubRefused as e:
        got = {"error": e.code}
    assert got == c["expect"]


@pytest.mark.parametrize("c", cases("msg2_build"), ids=lambda c: c["name"])
def test_noise_vectors_replayed_msg2(c):
    a = c["args"]
    try:
        hs = noise.read_msg1(bytes.fromhex(a["prologue_hex"]), X[a["wrk"]], bytes.fromhex(a["msg1_hex"]))
        msg2, tp = noise.write_msg2(hs, X[a["host_e"]], bytes.fromhex(a["challenge_hex"]))
        got: dict[str, Any] = {"msg2_hex": msg2.hex(), "handshake_hash_hex": tp.hh.hex()}
    except HubRefused as e:
        got = {"error": e.code}
    assert got == c["expect"]


def _transport(t: dict[str, Any]) -> noise.Transport:
    c = noise._Cipher  # noqa: SLF001
    return noise.Transport(
        bytes.fromhex(t["handshake_hash_hex"]), c(bytes.fromhex(t["k_i2r_hex"])), c(bytes.fromhex(t["k_r2i_hex"]))
    )


@pytest.mark.parametrize("c", cases("transport"), ids=lambda c: c["name"])
def test_noise_vectors_replayed_transport(c):
    """The host opens what the device sealed (i2r) and seals what the device opens (r2i), at the vector's nonce."""
    a = c["args"]
    tp = _transport(T[a["transcript"]])
    cipher = tp._in if a["dir"] == "i2r" else tp._out  # noqa: SLF001
    cipher.n = a["n"]
    ct = bytes.fromhex(a["ct_hex"])
    if "pt_hex" in c["expect"]:
        pt = bytes.fromhex(c["expect"]["pt_hex"])
        if a["dir"] == "i2r":
            assert tp.open(ct) == pt
        else:
            assert tp.seal(pt) == ct
    else:  # a refusal: a cipher of that direction and nonce does not open the frame
        with pytest.raises(HubRefused) as e:
            cipher._go(b"", ct, False)  # noqa: SLF001
        assert e.value.code == c["expect"]["error"]


def test_noise_wipe_zeroes_keys_and_refuses_everything_after():
    tp = _transport(T["session"])
    keys = (tp._in.k, tp._out.k)  # noqa: SLF001
    tp.wipe()
    assert all(k is None for k in (tp._in.k, tp._out.k)) and tp.hh == b""  # noqa: SLF001
    assert all(not any(k) for k in keys)  # the bytearrays themselves are zeroed
    with pytest.raises(HubRefused):
        tp.seal(b"x")
    with pytest.raises(HubRefused):
        tp.open(b"x" * 20)


def test_noise_transport_bounds():
    tp = _transport(T["session"])
    assert len(tp.seal(bytes(noise.PLAIN_MAX))) == 65535
    with pytest.raises(HubRefused) as e:
        tp.seal(bytes(noise.PLAIN_MAX + 1))
    assert e.value.code == "malformed"


# --- session.py on the vector workspace (fake service) ------------------------------------------------------------------  # noqa: E501


class Store:
    def __init__(self, remote_state: dict[str, Any], owners: set[str]) -> None:
        members = {p: SimpleNamespace(role="owner") for p in owners}
        self.state = SimpleNamespace(workspace=SimpleNamespace(remote=remote_state, members=members))
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self):
        with self.lock:
            yield


def fake(device_ids: tuple[str, ...] = (IDS["device_a"],), *, person: str | None = None, on: bool = True) -> Any:
    devs = {i: {k: v for k, v in LIST[i].items() if k != "device_id"} for i in device_ids}
    owners = {d["person"] for d in devs.values()}
    svc = SimpleNamespace(
        store=Store({"on": on, "until_ms": None, "level": "terminals", "devices": devs}, owners),
        clock=lambda: NOW / 1000,
        workspace_id=IDS["workspace_id"],
    )
    svc.submit = lambda fn, *a: _done(fn(*a))
    adapter.claim(svc, person or next(iter(owners)), None)
    return svc


def _done(v: Any) -> Future:
    f: Future = Future()
    f.set_result(v)
    return f


@pytest.fixture
def vec(monkeypatch):
    """Patches the three things a fake service cannot give: the WRK file, the host-appended lines, the counters."""
    log: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(wrk, "load", lambda svc: X["wrk"])
    monkeypatch.setattr(remote, "append_host", lambda svc, typ, **kw: log.append((typ, kw)))
    held: dict[str, int] = {}
    monkeypatch.setattr(counters, "held", lambda svc, did: held.get(did, 0))
    monkeypatch.setattr(counters, "record", lambda svc, did, n: held.__setitem__(did, n))
    return SimpleNamespace(log=log, held=held)


def start(
    svc: Any,
    sid: str,
    did: str,
    kind: str = "session",
    hub: str = IDS["hub_id"],
    *,
    e: str = "host_e",
    ch: str | None = None,
):
    reg = session.registry(svc)
    seq = iter([X[e], bytes.fromhex(ch or T["session"]["challenge_hex"])])
    reg.rand = lambda n: next(seq)
    t = gate.table(svc)
    t.register(sid, did, kind=kind)
    assert session.start(svc, sid, did, kind, hub, NOW)
    return reg.get(sid), t


def test_session_transcript_through_the_real_session(vec, monkeypatch):
    monkeypatch.setattr(session, "HOST_API", (1, 3))  # the transcript's host speaks up to API 3; the real host 1
    t = T["session"]
    svc = fake()
    s, tab = start(svc, t["stream_id"], t["id"])
    s.feed(bytes.fromhex(t["msg1_hex"]), NOW)
    assert [d.hex() for _, d in s.drain()] == [t["msg2_hex"]]
    s.feed(bytes.fromhex(t["first_hex"]), NOW + 1000)  # the open message of the transcript, sealed at nonce 0
    assert s.state == "live" and tab.is_open(t["stream_id"])
    assert [d.hex() for _, d in s.drain()] == [t["reply_hex"]]  # {"t":"opened","api":3}
    assert adapter.current(svc).take(10) == [
        {
            "t": "open", "stream_id": t["stream_id"], "api": 3, "workspace_id": IDS["workspace_id"],
            "device_id": t["id"], "person": LIST[t["id"]]["person"], "level": "work",
        }
    ]  # fmt: skip
    assert [x[0] for x in vec.log] == ["remote.session_opened"]


@pytest.mark.parametrize(
    "name, over",
    [
        ("msg1_replayed_into_other_stream", {"sid": "ab" * 16}),
        ("msg1_replayed_into_other_workspace", {"ws": IDS["workspace_id_2"]}),
        ("msg1_replayed_for_other_device", {"did": IDS["device_b"]}),
        ("msg1_replayed_via_other_hub", {"hub": IDS["hub_id_2"]}),
    ],
)
def test_handshake_other_workspace_device_stream_hub_refused(vec, name, over):
    """The transcript's message 1, replayed into a stream whose prologue differs in one field, closes ``handshake``
    (vectors ``msg1_replayed_*``); the host answers nothing."""
    t = T["session"]
    svc = fake((IDS["device_a"], IDS["device_b"]))
    if "ws" in over:
        svc.workspace_id = over["ws"]
    s, tab = start(svc, over.get("sid", t["stream_id"]), over.get("did", t["id"]), hub=over.get("hub", IDS["hub_id"]))
    s.feed(bytes.fromhex(t["msg1_hex"]), NOW)
    assert s.state == "closed" and s.reason == "handshake" and s.drain() == [] and not tab.is_open(s.sid)
    assert tab.take_closing() == [(s.sid, "handshake")]


@pytest.mark.parametrize("name", ["msg1_with_early_data", "msg1_truncated"])
def test_msg1_with_early_data_or_cut_is_malformed(vec, name):
    c = next(c for c in cases("msg1") if c["name"] == name)
    a = c["args"]["prologue"]
    s, tab = start(fake(), a["stream_id"], a["id"])
    s.feed(bytes.fromhex(c["args"]["msg1_hex"]), NOW)
    assert s.reason == "malformed" and tab.take_closing() == [(s.sid, "malformed")]


def test_a_replayed_message_1_costs_only_handshake_budget_and_gets_a_fresh_answer(vec):
    """The same message 1 into two sessions that carry its prologue is answered twice, each time with a fresh
    ephemeral and challenge (contract: a replayed message 1 costs only handshake budget)."""
    t = T["session"]
    answers = []
    for ch in ("11" * 32, "22" * 32):
        s, _ = start(fake(), t["stream_id"], t["id"], ch=ch)
        s.feed(bytes.fromhex(t["msg1_hex"]), NOW)
        ((_, msg2),) = s.drain()
        answers.append(msg2)
    assert answers[0] != answers[1] and all(len(a) == 80 for a in answers)


def test_static_pin_other_key_closed(vec):
    """A valid message 1 from a key that is not the entry's ``transport_pub`` closes ``handshake`` before any answer."""
    t = T["session"]
    svc = fake((IDS["device_a"], IDS["device_b"]))
    sid = t["stream_id"]
    pro = RP.prologue("session", IDS["workspace_id"], IDS["device_b"], sid, IDS["hub_id"])
    _, m1 = RP.initiator1(pro, X["dev_a_s"], X["dev_a_e"], RP.xpub(X["wrk"]))  # device A's key into B's entry
    s, tab = start(svc, sid, IDS["device_b"])
    s.feed(m1, NOW)
    assert s.reason == "handshake" and s.drain() == []
    # the right key opens
    pro2 = RP.prologue("session", IDS["workspace_id"], IDS["device_b"], "ee" * 16, IDS["hub_id"])
    _, ok1 = RP.initiator1(pro2, X["dev_b_s"], X["dev_b_e"], RP.xpub(X["wrk"]))
    s2, _ = start(svc, "ee" * 16, IDS["device_b"])
    s2.feed(ok1, NOW)
    assert s2.state == "open" and len(s2.drain()) == 1


@pytest.mark.parametrize("c", cases("open"), ids=lambda c: c["name"])
def test_open_vectors_through_the_session_admit(vec, c):
    """Every ``open_*`` vector against the session's own admission (presence over the open bytes, the counter CAS
    shared with the presence batches): a refusal is the stream's ``presence`` close, a pass records the counter."""
    a = c["args"]
    did = a["id"]
    svc = fake((IDS["device_a"], IDS["device_b"]))
    entry = svc.store.state.workspace.remote["devices"][did]
    entry["presence_kind"], entry["presence_pub"] = a["entry"]["presence_kind"], a["entry"]["presence_pub"]
    vec.held[did] = a["held"]
    s, _ = start(svc, "11" * 16, did)
    s.state = "open"
    # the vector binds ``workspace_id``: a vector for another workspace is checked against that workspace
    svc.workspace_id = a["workspace_id"]
    msg = {"t": "open", "api": [1, 3], "presence": a["assertion"]}
    if "count" in c["expect"]:
        s._admit(msg, bytes.fromhex(a["challenge_hex"]), bytes.fromhex(a["handshake_hash_hex"]), 1, NOW)  # noqa: SLF001
        assert s.state == "live"
        if entry["presence_kind"] == "fido2":
            assert vec.held[did] == c["expect"]["count"]
    else:
        with pytest.raises(session._Fail) as e:  # noqa: SLF001
            s._admit(msg, bytes.fromhex(a["challenge_hex"]), bytes.fromhex(a["handshake_hash_hex"]), 1, NOW)  # noqa: SLF001
        # shape errors of the assertion are as final as a bad signature: the close reason is ``presence``
        assert e.value.reason == "presence" and s.state == "open"
        assert vec.held.get(did, 0) == a["held"]  # nothing recorded


@pytest.mark.parametrize("c", cases("open_msg"), ids=lambda c: c["name"])
def test_open_msg_vectors(c):
    a = c["args"]
    try:
        got: dict[str, Any] = {"api": session.check_open_msg(a["msg"], tuple(a["host_api"]))}
    except HubRefused as e:
        got = {"error": e.code}
    assert got == c["expect"]


# --- app frames: the host's light tracker ---------------------------------------------------------------------------------  # noqa: E501


def _step(tr: session.Tracker, d: str, pt: bytes) -> str:
    kind, i = session.app_header(pt)
    return tr.step(d, kind, i, len(pt) - 5)


def test_tracker_ids_rise_and_exchanges_are_counted():
    tr = session.Tracker()
    for i in range(1, 9):
        assert _step(tr, "d2h", head(i)) == "ok"
    assert _step(tr, "d2h", head(9)) == "reset:remote.busy"  # the ninth exchange
    with pytest.raises(session._Fail):  # noqa: SLF001 - a lower or reused id
        _step(tr, "d2h", head(5))
    assert _step(tr, "d2h", frame("RESET", 3, b"cancelled")) == "ok"
    assert _step(tr, "d2h", head(10)) == "ok"
    assert _step(tr, "d2h", frame("BODY", 3, b"x")) == "drop"  # an ended exchange: dropped, not an error
    with pytest.raises(session._Fail):  # noqa: SLF001 - a frame for an id above every opened one that opens nothing
        _step(tr, "d2h", frame("BODY", 99, b"x"))


def test_tracker_websocket_limit_and_body_limit_and_ping_pairing():
    tr = session.Tracker()
    ws = frame("WS_OPEN", 1, b"{}")
    assert _step(tr, "d2h", ws) == "ok" and _step(tr, "d2h", frame("WS_OPEN", 2, b"{}")) == "ok"
    assert _step(tr, "d2h", frame("WS_OPEN", 3, b"{}")) == "reset:remote.busy"
    assert _step(tr, "d2h", head(4)) == "ok"
    assert _step(tr, "d2h", frame("BODY", 4, bytes(60000))) == "ok"
    for _ in range(16):  # 17 x 60000 = 1 020 000 bytes in all: under 1 MiB
        assert _step(tr, "d2h", frame("BODY", 4, bytes(60000))) == "ok"
    assert _step(tr, "d2h", frame("BODY", 4, bytes(60000))) == "reset:too_large"  # the 18th crosses 1 MiB
    assert _step(tr, "d2h", frame("PING", 0)) == "ok"
    with pytest.raises(session._Fail):  # noqa: SLF001 - a second PING before the PONG
        _step(tr, "d2h", frame("PING", 0))
    assert _step(tr, "h2d", frame("PONG", 0)) == "ok"
    with pytest.raises(session._Fail):  # noqa: SLF001 - a PONG nobody asked for
        _step(tr, "d2h", frame("PONG", 0))


def test_app_header_rules():
    for bad in (
        b"",
        b"\x01\0\0\0",
        frame("HEAD", 1, b"")[:0] + bytes([11, 0, 0, 0, 1]),
        frame("PING", 1),
        frame("HEAD", 0, b"{}"),
        frame("BODY", 1, b""),
        frame("END", 1, b"x"),
        frame("RESET", 1, b"nope"),
        frame("WS_CLOSE", 1, b"\x03\xe7"),
        frame("WS_CLOSE", 1, b"\x03"),
        bytes(65520),
    ):
        with pytest.raises(session._Fail):  # noqa: SLF001
            session.app_header(bad)
    assert session.app_header(frame("WS_CLOSE", 7, b"\x03\xe8")) == (8, 7)
    assert session.app_header(frame("PING", 0)) == (9, 0)
    assert session.app_header(frame("BODY", 1, bytes(65514))) == (2, 1)


# --- a real served workspace -----------------------------------------------------------------------------------------------  # noqa: E501


def test_a_full_session_opens_and_forwards_a_request_with_the_hosts_identity(human, served):
    w = World(human, served)
    c = w.client()
    assert c.connect() == {"t": "opened", "api": 1}
    note = w.ad.take(10)
    assert note == [
        {
            "t": "open",
            "stream_id": c.sid,
            "api": 1,
            "workspace_id": w.ws,
            "device_id": w.did,
            "person": w.person,
            "level": "work",
        }
    ]
    assert [e["type"] for e in w.log("remote.session_opened")] == ["remote.session_opened"]
    assert w.log("remote.session_opened")[0]["person"] == w.person
    c.say(head(1))
    c.say(frame("END", 1))
    got = w.ad.take(10)
    assert [m["t"] for m in got] == ["frame", "frame"]
    assert got[0]["identity"] == {"workspace_id": w.ws, "device_id": w.did, "person": w.person, "level": "work"}
    assert "identity" not in got[1] and unb64u(got[0]["bytes"]) == head(1)


def test_ping_is_answered_by_the_host_and_never_forwarded(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    w.ad.take(10)
    c.say(frame("PING", 0))
    assert c.hear() == [frame("PONG", 0)] and w.ad.take(10) == []


def test_identity_ignores_what_the_device_sends(human, served):
    """The device's bytes cannot name a level or a person: the host's identity is the only one the adapter gets."""
    w = World(human, served, level="work", cap="view")
    c = w.client()
    c.connect()
    w.ad.take(10)
    forged = dumps(
        {"headers": [["x-orch-level", "terminals"]], "method": "GET", "path": "/", "query": "level=terminals"}
    )
    c.say(frame("HEAD", 1, forged))
    (m,) = w.ad.take(10)
    assert m["identity"]["level"] == "view" and m["identity"]["person"] == w.person  # min(device cap, policy level)


def test_open_key_file_and_up_only_fido2_and_be_bs_refused(human, served):
    w = World(human, served, kind="fido2", cap="work")

    def attempt(**kw):
        c = w.client()
        assert c.open_stream() is None
        c.handshake()
        a = w.dev.assertion(RP.open_bytes(w.ws, w.did, c.hh, c.challenge), **kw)
        c.say(dumps({"t": "open", "api": [1, 3], "presence": a}))
        return c

    for name, kw in (
        ("up_only", {"flags": 0x01}),
        ("uv_without_up", {"flags": 0x04}),
        ("backup_eligible", {"flags": 0x0D}),
        ("backup_state", {"flags": 0x15}),
        ("extension_data", {"flags": 0x85}),
    ):
        c = attempt(**kw)
        assert w.link.closes(c.sid) == ["presence"], name
    key_file = w.client()
    key_file.open_stream()
    key_file.handshake()
    key_file.say(dumps({"t": "open", "api": [1, 3], "presence": {"sig": b64u(bytes(64))}}))  # a key file has no form
    assert w.link.closes(key_file.sid) == ["presence"]
    wrong_form = attempt(form="p256")  # a FIDO2 entry never accepts the p256 form
    assert w.link.closes(wrong_form.sid) == ["presence"]
    ok = attempt(count=1)
    assert w.sess(ok.sid).state == "live" and w.link.closes(ok.sid) == []


def test_open_counter_cas_shared_with_presence(human, served):
    w = World(human, served, kind="fido2")
    c = w.client()
    w.dev.count = 4
    c.open_stream()
    c.handshake()
    c.say(
        dumps(
            {
                "t": "open",
                "api": [1, 3],
                "presence": w.dev.assertion(RP.open_bytes(w.ws, w.did, c.hh, c.challenge), count=5),
            }
        )
    )
    assert w.sess(c.sid).state == "live" and counters.held(w.service, w.did) == 5
    # the presence batches CAS the same counter: 5 is spent, 6 is fresh
    with pytest.raises(HubRefused) as e:
        counters.cas(w.service, w.did, 5)
    assert e.value.code == "replay"
    assert counters.cas(w.service, w.did, 6) == 6
    d = w.client()  # a new open at counter 6 is a replay, at 7 it passes
    d.open_stream()
    d.handshake()
    d.say(
        dumps(
            {
                "t": "open",
                "api": [1, 3],
                "presence": w.dev.assertion(RP.open_bytes(w.ws, w.did, d.hh, d.challenge), count=6),
            }
        )
    )
    assert w.link.closes(d.sid) == ["presence"] and counters.held(w.service, w.did) == 6
    e2 = w.client()
    e2.open_stream()
    e2.handshake()
    e2.say(
        dumps(
            {
                "t": "open",
                "api": [1, 3],
                "presence": w.dev.assertion(RP.open_bytes(w.ws, w.did, e2.hh, e2.challenge), count=7),
            }
        )
    )
    assert w.sess(e2.sid).state == "live" and counters.held(w.service, w.did) == 7


def test_challenge_single_use_60s(human, served):
    w = World(human, served)
    late = w.client()
    late.open_stream()
    late.handshake()
    w.advance(61)  # the challenge is valid 60 s from message 2
    late.say(dumps(late.open_msg()))
    assert w.link.closes(late.sid) == ["handshake"]
    c = w.client()
    c.open_stream()
    c.handshake()
    w.advance(59)
    c.say(dumps(c.open_msg()))
    assert w.sess(c.sid).state == "live"
    c.say(dumps(c.open_msg()))  # the same challenge again on the live stream: not an app frame
    assert w.link.closes(c.sid) == ["malformed"]


def test_a_challenge_is_consumed_even_by_a_failed_open(human, served):
    w = World(human, served)
    c = w.client()
    c.open_stream()
    c.handshake()
    s = w.sess(c.sid)
    c.say(dumps(c.open_msg(presence={"form": "p256", "sig": b64u(bytes(64))})))
    assert w.link.closes(c.sid) == ["presence"] and s.challenge is None


def test_device_of_non_owner_never_opens(human, served):
    from tests.host.test_remote_ops import _host_event

    w = World(human, served)
    p2 = served.lab.add_member()
    _host_event(served, served.lab.ws.owner, "role.changed", person=p2.ref, role="owner")
    c = w.client()
    c.open_stream()
    c.handshake()  # the stream passed the gate while its person was an owner
    _host_event(served, p2, "role.changed", person=served.lab.ws.owner.ref, role="member")
    c.say(dumps(c.open_msg()))
    assert w.log("remote.session_opened") == [] and w.link.closes(c.sid) in (["remote.device"], ["revoked"])
    late = w.client()
    assert late.open_stream() == "remote.device"  # and the gate refuses it at the door from then on


def test_device_person_must_be_adapter_person(human, served):
    from tests.host.test_remote_ops import _host_event

    w = World(human, served)
    p2 = served.lab.add_member()
    _host_event(served, served.lab.ws.owner, "role.changed", person=p2.ref, role="owner")
    adapter.release(w.service)
    w.ad = adapter.claim(w.service, p2.ref, None)  # the dashboard of owner B; the device belongs to owner A
    c = w.client()
    c.open_stream()
    c.handshake()
    s = w.sess(c.sid)
    c.say(dumps(c.open_msg()))
    assert w.link.closes(c.sid) == ["remote.device"] and w.log("remote.session_opened") == []
    assert s.state == "closed"


def test_no_adapter_answers_remote_version(human, served):
    w = World(human, served)
    adapter.release(w.service)
    c = w.client()
    assert c.open_stream() == "remote.version"
    assert w.table().count_workspace() == 0 and w.sess(c.sid) is None
    w.ad = adapter.claim(w.service, w.person, None)
    w.keep_lease = False
    w.advance(16)  # the dashboard stopped polling: the lease ran out
    assert w.client().open_stream() == "remote.version"


def test_a_lapsed_lease_closes_the_open_sessions(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    w.keep_lease = False
    w.advance(16)
    w.pump()
    assert w.link.closes(c.sid) == ["closed"]


@pytest.fixture
def no_commit_close(monkeypatch):
    """The commit hook normally closes the stream first; here only the per-request check may stop a request."""
    monkeypatch.setattr(gate.StreamTable, "on_commit", lambda self, log, event: None)


def _second_head_is_never_forwarded(w: World, c, want: str) -> None:
    c.say(head(2))
    assert [m for m in w.ad.take(10) if m["t"] == "frame"] == []
    assert w.link.closes(c.sid) == [want]


@pytest.mark.parametrize("what", ["off", "device_removed", "owner_demoted"])
def test_per_request_recheck_off_level_device(human, served, no_commit_close, what):
    """Between two HEADs the policy goes off, the device is removed or its person stops being an owner: the second
    HEAD is never forwarded and the stream ends (``remote.off`` / ``revoked``)."""
    from tests.host.test_remote_ops import _host_event

    w = World(human, served)
    c = w.client()
    c.connect()
    w.ad.take(10)
    c.say(head(1))
    assert [m["t"] for m in w.ad.take(10)] == ["frame"]
    if what == "off":
        run_on(served, remote.policy.turn_off, served.service, "owner")
    elif what == "device_removed":
        run_on(served, remote.devices.remove, served.service, w.did, "owner")
    else:
        p2 = served.lab.add_member()
        _host_event(served, served.lab.ws.owner, "role.changed", person=p2.ref, role="owner")
        _host_event(served, p2, "role.changed", person=served.lab.ws.owner.ref, role="member")
    _second_head_is_never_forwarded(w, c, "remote.off" if what == "off" else "revoked")


def test_per_request_recheck_level_follows_the_policy_and_the_end_date(human, served, no_commit_close):
    w = World(human, served)
    c = w.client()
    c.connect()
    w.ad.take(10)
    human.run("remote.set", remote_args(level="view"))  # a narrowing policy: the level is recomputed per request
    c.say(head(1))
    (m,) = w.ad.take(10)
    assert m["identity"]["level"] == "view"
    s = w.sess(c.sid)
    past = clock_ms(served) + 4 * 86_400_000  # the policy's end date (3 days) has passed
    with pytest.raises(session._Fail) as e:  # noqa: SLF001
        run_on(served, s._facts, past)  # noqa: SLF001
    assert e.value.reason == "remote.off"


def test_revoke_inside_store_lock_wipes_keys(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    s = w.sess(c.sid)
    seen: list[bool] = []
    real = s.close

    def spy(reason: str) -> None:
        seen.append(served.service.store.lock_held())
        real(reason)

    w.table().attach(c.sid, spy)
    tp = s.tp
    assert call(served, "remote.off")["ok"]
    assert seen == [True] and s.state == "closed" and s.reason == "remote.off"
    assert tp.wiped and tp._in.k is None and s.challenge is None  # noqa: SLF001
    with pytest.raises(HubRefused):
        tp.seal(b"x")
    c.say(head(1))  # later frames are dropped
    assert [m for m in w.ad.take(10) if m["t"] == "frame"] == []
    assert w.link.closes(c.sid) == ["remote.off"]
    assert [m["t"] for m in w.ad.take(10)] == []  # the closed notice was taken above with the open one


def test_close_notifies_the_adapter_and_logs_session_closed_after_the_lock(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    w.ad.take(10)
    call(served, "remote.off")
    assert w.ad.take(10) == [{"t": "closed", "stream_id": c.sid, "reason": "remote.off"}]
    w.pump()  # the pump appends the closing line once the lock is released
    (line,) = w.log("remote.session_closed")
    assert (line["stream_id"], line["device_id"], line["reason"]) == (c.sid, w.did, "remote.off")
    w.pump()
    assert len(w.log("remote.session_closed")) == 1


def test_idle_2h_life_12h_handshake_60s(human, served):
    w = World(human, served)
    slow = w.client()  # a stream that never sends its message 1
    slow.open_stream()
    idle, life = w.client(), w.client()
    idle.connect()
    life.connect()
    s_idle, s_life = w.sess(idle.sid), w.sess(life.sid)
    w.advance(61)
    w.pump()
    assert w.link.closes(slow.sid) == ["handshake"] and s_idle.state == "live"  # 60 s is the handshake's alone
    for n in range(3):  # a device that sends a request every 40 min is never idle; the other one is, after 2 h
        w.advance(40 * 60)
        life.say(frame("PING", 0))  # keepalives do not count
        life.say(head(1 + n))
        w.ad.take(100)
    w.advance(61)
    w.pump()
    assert w.link.closes(idle.sid) == ["idle"] and s_life.state == "live"
    w.advance(11 * 3600)  # recently active or not, a stream lives 12 h at most
    w.pump()
    assert w.link.closes(life.sid) == ["expired"]


def test_pings_pongs_and_frames_for_ended_exchanges_do_not_keep_a_session_alive(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    s = w.sess(c.sid)
    w.ad.take(10)
    c.say(head(1))
    c.say(frame("RESET", 1, b"cancelled"))
    w.advance(2 * 3600 - 60)
    w.pump()
    assert s.state == "live"  # the last accepted frame was 2 h - 1 min ago
    w.advance(120)
    c.say(frame("BODY", 1, b"x"))  # an ended exchange: dropped, does not count
    c.say(frame("PING", 0))
    assert w.link.closes(c.sid) == ["idle"]


def test_bad_app_frames_close_malformed(human, served):
    w = World(human, served)
    for bad in (frame("BODY", 9, b"x"), b"\x0b\0\0\0\x01", frame("PING", 3), head(0)):
        c = w.client()
        c.connect()
        c.say(bad)
        assert w.link.closes(c.sid) == ["malformed"], bad
    c = w.client()
    c.connect()
    c.send(b"\x00" * 40)  # not a Noise message of this stream at all
    assert w.link.closes(c.sid) == ["handshake"]


def test_a_request_body_over_1mib_is_reset_and_the_adapter_told(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    w.ad.take(10)
    c.say(head(1))
    for _ in range(18):
        c.say(frame("BODY", 1, bytes(60000)))
    sent = c.hear()
    assert frame("RESET", 1, b"too_large") in sent
    assert frame("RESET", 1, b"too_large") in [unb64u(m["bytes"]) for m in w.ad.take(100) if m["t"] == "frame"]
    assert w.sess(c.sid).state == "live"  # the stream stays; only the exchange ended


def test_ninth_exchange_is_reset_busy_and_not_forwarded(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    w.ad.take(10)
    for i in range(1, 10):
        c.say(head(i))
    assert len([m for m in w.ad.take(100) if m["t"] == "frame"]) == 8
    assert c.hear() == [frame("RESET", 9, b"remote.busy")]


def test_device_frames_after_an_enroll_stream_enrolled_are_malformed_and_the_wait_is_off_the_pump(human, served):
    """An enroll stream: handshake with the code id, the first message through ``enrollment.verify`` on the pump
    thread, ``pending`` to the device, and the owner's confirmation (a person op) ends it with ``added`` then
    ``closed`` from a worker thread, never the pump."""
    from tests.host.test_remote_enroll import Device as EnrollDevice
    from tests.host.test_remote_enroll import new_code

    w = World(human, served)
    code = new_code(served)
    dev = EnrollDevice("Phone", seed=9)
    scalar = dev.transport  # the test device's enroll helper uses the 32 bytes as its transport key; make them real
    dev.transport = RP.xpub(scalar)
    sid = "ee" * 16
    assert w.link.frames == []
    w.link.frames.append(("stream_open", 1, {"stream_id": sid, "device_id": code["code_id"], "kind": "enroll"}))
    w.pump()
    assert w.link.answers[-1] == (1, None)
    pro = RP.prologue("enroll", w.ws, code["code_id"], sid, HUB)
    ini, m1 = RP.initiator1(pro, scalar, bytes([77]) * 32, w.wrk_pub)
    n = [0]

    def send(raw):
        n[0] += 1
        w.link.frames.append(("stream_data", 0, {"stream_id": sid, "seq": n[0], "bytes": b64u(raw)}))
        w.pump()

    send(m1)
    ch = RP.initiator2(ini, w.link.data(sid)[0])
    first = dev.first(served, code["code"], code["code_id"], ini.hh, ch)
    send(ini.c_i2r.seal(b"", dumps(first)))
    pending = loads(ini.c_r2i.open(b"", w.link.data(sid)[1]))
    assert pending["t"] == "pending" and len(pending["fingerprint"]) == 64 and set(pending) == {"t", "fingerprint"}
    assert w.sess(sid).state == "pending"
    send(ini.c_i2r.seal(b"", dumps({"t": "enroll"})))  # anything after the enroll message is malformed
    assert w.link.closes(sid) == ["malformed"]


def loads(b: bytes) -> Any:
    from orch.canon.jcs import loads_strict

    return loads_strict(b)


def test_wrk_never_leaves_host(human, served):
    w = World(human, served)
    scalar = wrk.load(w.service)
    needles = [scalar, scalar.hex().encode(), b64u(scalar).encode()]
    c = w.client()
    c.connect()
    c.say(head(1))
    w.ad.take(100)
    c.say(head(2))
    blob = repr(w.link.sent).encode() + repr(w.ad.take(100)).encode()
    for op in ("remote.status", "remote.sessions", "remote.pending"):
        blob += json.dumps(call(served, op)).encode()
    blob += repr(w.log("remote.session_opened")).encode() + repr(session.registry(w.service).all()[0].__dict__).encode()
    for n in needles:
        assert n not in blob


def test_sweep_and_pump_do_not_run_on_a_closed_session(human, served):
    w = World(human, served)
    c = w.client()
    c.connect()
    gate.table(w.service).close(c.sid, "revoked")
    w.pump()
    w.advance(13 * 3600)
    w.pump()
    assert w.link.closes(c.sid) == ["revoked"]  # one close frame only


@pytest.fixture(autouse=True)
def _no_threads_left():
    yield
    for t in threading.enumerate():
        if t.name == "remote-enroll-wait":
            t.join(timeout=2)
