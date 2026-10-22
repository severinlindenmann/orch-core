"""C12: the v2 ws->ws envelope (protocol-v2 section 11) in core: the vendored relay vectors replayed through
``receive`` (co-signature cases are refused by design: fail closed), plus round trips, the size cap and tampering."""

from __future__ import annotations

import pytest

from orch.crypto import suite
from orch.crypto.encoding import b64u, unb64u
from orch.hubclient import wsenvelope as W
from tests.relay.corpus import S2

CASES = {c["name"]: c for c in S2["ws_envelopes"]["cases"]}
COSIGNED_OK = {"cosigned_by_device_key", "cosigned_by_webauthn"}  # positives need the chain check: refused here


def _steps(c: dict) -> list[dict]:
    return c.get("steps") or [{"envelope": c["envelope"], "now_ms": c["now_ms"], "expect": c["expect"]}]


def _mailbox(c: dict) -> tuple[W.Mailbox, dict, object]:
    st = c["state"]
    mb = W.Mailbox()
    other = suite.public_bytes(suite.generate_private_key())  # the vectors' pins carry no WXK: only needed to reply
    for ws, p in st["peers"].items():
        mb.pin_peer(ws, unb64u(p["wsk_pub"], 65), other, 1)
    mb.sent = {k: {**v, "state": "waiting"} for k, v in st["sent"].items()}
    keys = {}
    for v, k in st["wxk"].items():
        d = int.from_bytes(bytes.fromhex(k["seed"]), "big") % (suite.P256_N - 1) + 1
        keys[int(v)] = (suite.private_key_from_scalar(d), k["retired_ms"])
    return mb, st, keys


def _matches(o: W.Outcome, expect: dict) -> bool:
    want = expect["result"]
    if want == "drop":
        return o.action == "drop" and o.code == expect["why"]
    if want in ("refuse", "inbox", "deliver"):
        if o.action != want:
            return False
        if want == "refuse":
            return o.code == expect["code"]
        return True
    return o.action == "duplicate"


@pytest.mark.parametrize("name", [n for n in CASES if n not in COSIGNED_OK])
def test_relay_vectors(name: str) -> None:
    c = CASES[name]
    mb, st, keys = _mailbox(c)
    me_wsk = suite.generate_private_key()
    for step in _steps(c):
        o = W.receive(
            bytes.fromhex(step["envelope"]), mailbox=mb, me=st["workspace"],
            wxk_for=keys.get, wxk_current=st["wxk_version"], wsk=me_wsk, now_ms=step["now_ms"],
        )  # fmt: skip
        assert _matches(o, step["expect"]), (name, o, step["expect"])
        if step["expect"]["result"] == "refuse" and o.reply is not None:
            assert len(o.reply) < W.MAX_ENVELOPE


def test_cosigned_positive_vectors_are_refused_closed() -> None:
    for name in COSIGNED_OK:
        c = CASES[name]
        mb, st, keys = _mailbox(c)
        o = W.receive(
            bytes.fromhex(c["envelope"]), mailbox=mb, me=st["workspace"], wxk_for=keys.get,
            wxk_current=1, wsk=suite.generate_private_key(), now_ms=c["now_ms"],
        )  # fmt: skip
        assert (o.action, o.code) == ("refuse", "bad_cosignature")


# --- two workspaces, own keys -----------------------------------------------------------------------------------

A, B = "aa" * 16, "bb" * 16
NOW = 1_790_000_000_000


class Side:
    def __init__(self, ws: str) -> None:
        self.ws, self.wsk, self.wxk = ws, suite.generate_private_key(), suite.generate_private_key()
        self.mb = W.Mailbox()

    def pin(self, other: Side) -> None:
        self.mb.pin_peer(other.ws, suite.public_bytes(other.wsk), suite.public_bytes(other.wxk), 1)

    def recv(self, env: bytes, now: int = NOW) -> W.Outcome:
        return W.receive(
            env, mailbox=self.mb, me=self.ws, wxk_for=lambda v: (self.wxk, None) if v == 1 else None,
            wxk_current=1, wsk=self.wsk, now_ms=now,
        )  # fmt: skip


def pair() -> tuple[Side, Side]:
    a, b = Side(A), Side(B)
    a.pin(b)
    b.pin(a)
    return a, b


def ticket(a: Side, size: int = 0) -> W.Built:
    body = W.new_ticket_body({"title": "t", "pad": "x" * size}, deadline_ms=NOW + 3_600_000)
    return W.build_envelope(mailbox=a.mb, wsk=a.wsk, from_ws=A, to_ws=B, body=body)


def test_ticket_and_result_round_trip() -> None:
    a, b = pair()
    t = ticket(a)
    a.mb.sent[t.envelope_id] = {"to": B, "consumed": [], "state": "waiting"}
    o = b.recv(t.envelope)
    assert o.action == "inbox" and o.item["sha256"] == t.sha256 and o.item["ticket"]["title"] == "t"
    r = W.build_envelope(mailbox=b.mb, wsk=b.wsk, from_ws=B, to_ws=A,
                         body=W.new_result_body(t.envelope_id, {"summary": "done"}, deadline_ms=NOW + 1))  # fmt: skip
    got = a.recv(r.envelope)
    assert got.action == "deliver" and got.item["result"] == {"summary": "done"} and not got.item["late"]
    assert a.mb.sent[t.envelope_id]["state"] == "answered"
    second = W.new_result_body(t.envelope_id, {"summary": "x"}, deadline_ms=NOW)
    again = a.recv(W.build_envelope(mailbox=b.mb, wsk=b.wsk, from_ws=B, to_ws=A, body=second).envelope)
    assert (again.action, again.code) == ("refuse", "unknown_reply")


def test_one_mib_envelope_builds_and_over_cap_is_refused_before_sending() -> None:
    a, b = pair()
    big = ticket(a, size=W.MAX_ENVELOPE - 700)
    assert 1_000_000 < len(big.envelope) <= W.MAX_ENVELOPE
    assert b.recv(big.envelope).action == "inbox"
    with pytest.raises(W.EnvelopeError) as e:
        ticket(a, size=W.MAX_ENVELOPE)
    assert e.value.code == "too_large"


def test_unpinned_sender_is_dropped_and_never_answered() -> None:
    a, b = pair()
    t = ticket(a)
    b.mb.peers.clear()
    o = b.recv(t.envelope)
    assert (o.action, o.code, o.reply) == ("drop", "not_pinned", None)


def test_build_to_unknown_peer_refused() -> None:
    a = Side(A)
    with pytest.raises(W.EnvelopeError) as e:
        ticket(a)
    assert e.value.code == "unknown_peer"


def test_tampered_body_and_replayed_bytes() -> None:
    a, b = pair()
    t = ticket(a)
    bad = bytearray(t.envelope)
    bad[70] ^= 1
    assert b.recv(bytes(bad)).code == "signature"
    assert b.recv(t.envelope).action == "inbox"
    dup = b.recv(t.envelope, NOW + 5)
    assert dup.action == "duplicate" and len(b.mb.inbox) == 1


def test_mailbox_json_round_trip_and_strict() -> None:
    a, _ = pair()
    t = ticket(a)
    a.mb.sent[t.envelope_id] = {"to": B, "consumed": [], "state": "waiting"}
    a.mb.outbox[t.envelope_id] = b64u(t.envelope)
    assert W.Mailbox.from_json(a.mb.to_json()).to_json() == a.mb.to_json()
    for bad in (None, {}, {**a.mb.to_json(), "x": {}}, {**a.mb.to_json(), "peers": []}):
        with pytest.raises(W.EnvelopeError):
            W.Mailbox.from_json(bad)
