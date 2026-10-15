"""R4: the unpublished-evidence consumer (11A.9, relay protocol §19.5 P3-D31). A scripted session plays the relay;
the real store holds the evidence (``Store.note_forwarded_revocation``, H12b) for the end-to-end and adversarial
cases of the 11A.10 R4 list."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.identity import certs
from orch.relay import unpublished as up
from orch.relay.http import TransportError
from orch.relay.retry import RelayRefused
from orch.relay.session import Fenced
from orch.relay.state import StateDir
from orch.store import pins as pins_mod
from tests.identity.helpers import Person
from tests.store.helpers import T0, Env
from tests.store.test_forwarded_revocation import _cert, item

WS = "705d40abbb8c1c90354a1acaa94c935c"


def pos(c: int) -> str:
    return base64.urlsafe_b64encode(canon.dumps({"v": 1, "ws": WS, "c": c})).decode().rstrip("=")


class Relay:
    """A scripted relay behind a Session-shaped ``call``: ``pages[position]`` is ``(items, cursor, more)``; every
    call is recorded with what the state file held at that moment."""

    def __init__(self, state: StateDir, pages=None, *, gen: int | None = 1) -> None:
        self.state, self.pages, self.gen = state, dict(pages or {}), gen
        self.calls: list[tuple[str, str, object, object]] = []
        self.fail: dict[str, BaseException] = {}
        self.acked: list[str] = []
        self.kws: list[dict] = []

    def call(self, method, path, route, **kw):
        snap = self.state.read_json(up.FILE)
        self.calls.append((method, path, kw.get("body"), snap))
        self.kws.append(kw)
        if route in self.fail:
            e = self.fail.pop(route)
            raise e
        if method == "GET":
            q = path.split("?", 1)[1]
            params = dict(p.split("=", 1) for p in q.split("&"))
            items, cursor, more = self.pages.get(params.get("cursor"), ([], params.get("cursor") or pos(0), False))
            return {"items": items, "cursor": cursor, "more": more}
        self.acked.append(kw["body"]["cursor"])
        return {"acked": kw["body"]["cursor"]}


class Clock:
    def __init__(self) -> None:
        self.ms = T0 * 1000
        self.mono = 1000.0

    def now(self) -> int:
        return self.ms


@pytest.fixture
def state(tmp_path):
    s = StateDir(tmp_path / "relay")
    yield s
    s.close()


def make(state, relay, hold, *, notices=None, clock=None, alarms=None):
    clock = clock or Clock()
    alarms = alarms if alarms is not None else []
    c = up.UnpublishedConsumer(
        workspace_id=WS, session=relay, state=state, hold=hold, notices=lambda: notices() if notices else None,
        alarm=lambda code, detail: alarms.append((code, detail)), now_ms=clock.now, mono=lambda: clock.mono,
    )  # fmt: skip
    return c, clock, alarms


def _it(dev="ab" * 16, person="cd" * 16, reason="compromised", digest="sha256:" + "1" * 64):
    return {"device_id": dev, "person_id": person, "reason": reason, "provenance": {"cursor": 1, "digest": digest}}


# -- when and what it reads ---------------------------------------------------------------------------------------


def test_a_new_holder_generation_reads_from_the_start_page_by_page_then_acks_each(state):
    relay = Relay(state, {None: ([_it()], pos(1), True), pos(1): ([_it(dev="ef" * 16)], pos(2), False)})
    seen = []
    c, clock, _ = make(state, relay, lambda i: seen.append(i["device_id"]) or "pending")
    c.step()
    c.step()
    c.step()  # nothing left: no notice, the full read done
    assert seen == ["ab" * 16, "ef" * 16]
    gets = [p for m, p, _, _ in relay.calls if m == "GET"]
    assert gets[0].endswith("unpublished?limit=64") and gets[1].endswith(f"?cursor={pos(1)}&limit=64")
    assert relay.acked == [pos(1), pos(2)] and len(gets) == 2
    st = c.load()
    assert st["scan"] is None and st["cursor"] == pos(2) and st["gen"] == 1 and st["last_full_ms"] == clock.ms


def test_the_position_is_durable_before_each_ack_and_every_item_held_before(state):
    order = []
    relay = Relay(state, {None: ([_it(), _it(dev="ef" * 16)], pos(2), False)})
    c, _, _ = make(state, relay, lambda i: order.append(("hold", i["device_id"])) or "pending")
    c.step()
    ack = next(call for call in relay.calls if call[0] == "POST")
    assert ack[2] == {"cursor": pos(2)} and ack[3]["cursor"] == pos(2)  # written before the ack
    assert order == [("hold", "ab" * 16), ("hold", "ef" * 16)]


def test_nothing_is_read_without_a_holder_generation(state):
    relay = Relay(state, gen=None)
    c, _, _ = make(state, relay, lambda i: "pending")
    c.step()
    assert relay.calls == []


def test_after_the_full_read_only_a_new_notice_triggers_a_read(state):
    relay = Relay(state)
    note = {"unpublished": None}
    c, _, _ = make(state, relay, lambda i: "pending", notices=lambda: note)
    c.step()  # the full read (empty)
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n  # no notice
    note["unpublished"] = pos(5)
    relay.pages[pos(0)] = ([_it()], pos(5), False)
    c.step()
    assert any(f"cursor={pos(0)}" in p for _, p, _, _ in relay.calls[n:])
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n  # the same notice, already answered: no read again
    note["unpublished"] = pos(6)
    c.step()
    assert len(relay.calls) > n


def test_a_daily_full_read_and_a_new_generation_restart_from_the_start(state):
    relay = Relay(state)
    c, clock, _ = make(state, relay, lambda i: "pending")
    c.step()
    n = len(relay.calls)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")
    n = len(relay.calls)
    relay.gen = 2
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64") and c.load()["gen"] == 2


def test_a_new_generation_discards_a_crashed_predecessors_scan_even_mid_scan(state):
    relay = Relay(state, {None: ([_it()], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, _ = make(state, relay, lambda i: "held")
    c.step()  # gen 1: page 1 read, held, acked; the scan is mid-way
    mid = c.load()
    assert mid["scan"] == pos(1) and mid["scan_held"] and mid["gen"] == 1
    relay.acked.clear()
    relay.gen = 2  # the holder crashed; a NEW generation takes over
    n = len(relay.calls)
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")  # from the start, not from pos(1)
    after = c.load()
    assert after["gen"] == 2 and after["scan"] == pos(1) and [h["device_id"] for h in after["scan_held"]] == ["ab" * 16]
    assert relay.acked == [pos(1)]
    # the pair the predecessor held is no longer served (published meanwhile): the new scan must not inherit it
    relay.pages[None] = ([], pos(1), True)
    relay.gen = 3
    c.step()
    assert c.load()["scan_held"] == []


def test_the_same_generation_continues_its_scan(state):
    relay = Relay(state, {None: ([], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, _ = make(state, relay, lambda i: "pending")
    c.step()
    n = len(relay.calls)
    c.step()
    assert relay.calls[n][1].endswith(f"cursor={pos(1)}&limit=64")


ACK = "POST /v2/workspaces/{ws}/unpublished/ack"


def test_a_failed_ack_is_persisted_first_and_retried_after_backoff(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = TransportError("deadline", "test")
    c, clock, _ = make(state, relay, lambda i: "pending")
    c.step()
    post = next(call for call in relay.calls if call[0] == "POST")
    assert post[3]["ack"] == pos(1)  # durable before the send
    assert c.load()["ack"] == pos(1) and relay.acked == []
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n  # backoff: nothing sent yet
    clock.mono += up.BACKOFF_S + 1
    c.step()
    assert relay.acked == [pos(1)] and c.load()["ack"] is None
    assert [call[0] for call in relay.calls[n:]] == ["POST"]  # the retry is the ack, not a new read


def test_a_pending_ack_is_retried_after_a_restart(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = RelayRefused(503, "unavailable")
    c, _, _ = make(state, relay, lambda i: "pending")
    c.step()
    assert relay.acked == [] and c.load()["ack"] == pos(1)
    c2, _, _ = make(state, relay, lambda i: "pending")  # a new process: no in-memory backoff
    n = len(relay.calls)
    c2.step()
    assert relay.acked == [pos(1)] and c2.load()["ack"] is None
    assert [call[0] for call in relay.calls[n:]] == ["POST"]


def test_a_pending_ack_is_not_cleared_until_the_relay_accepted_it(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = RelayRefused(429, "rate_limited", retry_after=30)
    c, clock, alarms = make(state, relay, lambda i: "pending")
    c.step()
    assert c.load()["ack"] == pos(1) and alarms[0][0] == "unpublished_rate_limited"
    clock.mono += 29
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n and c.load()["ack"] == pos(1)


def test_a_fence_error_on_the_ack_stops_the_worker_and_keeps_the_ack_owed(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = Fenced("fenced")
    c, _, _ = make(state, relay, lambda i: "pending")
    with pytest.raises(Fenced):
        c.step()
    assert c.load()["ack"] == pos(1)


def test_a_pending_ack_of_an_older_generation_is_dropped_and_the_read_restarts(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = TransportError("deadline", "test")
    c, clock, _ = make(state, relay, lambda i: "pending")
    c.step()
    assert c.load()["ack"] == pos(1)
    relay.gen = 2
    clock.mono += up.BACKOFF_S + 1
    n = len(relay.calls)
    c.step()
    assert [call[0] for call in relay.calls[n:]] == ["GET", "POST"]  # re-read from the start, acked fresh
    assert relay.calls[n][1].endswith("unpublished?limit=64") and c.load()["ack"] is None


def test_cursor_ahead_on_the_ack_drops_it_and_restarts(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = RelayRefused(409, "cursor_ahead")
    c, _, _ = make(state, relay, lambda i: "pending")
    c.step()
    st = c.load()
    assert st["ack"] is None and st["scan"] == ""


# -- failures -----------------------------------------------------------------------------------------------------


def test_cursor_ahead_restarts_from_the_start(state):
    relay = Relay(state)
    note = {"unpublished": pos(9)}
    c, clock, alarms = make(state, relay, lambda i: "pending", notices=lambda: note)
    c.step()  # full read
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = RelayRefused(409, "cursor_ahead")
    note["unpublished"] = pos(10)
    c.step()
    assert c.load()["scan"] == "" and ("unpublished_cursor_ahead",) == alarms[-1][:1]
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n  # F4: backed off, the relay's answer does not set the pace
    clock.mono += up.BACKOFF_S + 1
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")


def test_rate_limited_waits_for_retry_after(state):
    relay = Relay(state)
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = RelayRefused(429, "rate_limited", retry_after=120)
    c, clock, alarms = make(state, relay, lambda i: "pending")
    c.step()
    n = len(relay.calls)
    c.step()
    clock.mono += up.BACKOFF_S + 60  # past the 30 s back-off, still inside Retry-After
    c.step()
    assert len(relay.calls) == n
    clock.mono += 31
    c.step()
    assert len(relay.calls) > n and alarms[0][0] == "unpublished_rate_limited"


def test_a_transport_error_backs_off_and_alarms_once(state):
    relay = Relay(state)
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = TransportError("deadline", "test")
    c, clock, alarms = make(state, relay, lambda i: "pending")
    c.step()
    clock.mono += up.BACKOFF_S + 1
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = TransportError("deadline", "test")
    c.step()
    assert [a[0] for a in alarms] == ["unpublished_transport"]


def test_a_store_fault_stores_and_acks_nothing(state):
    relay = Relay(state, {None: ([_it(), _it(dev="ef" * 16)], pos(2), False)})

    def hold(i):
        if i["device_id"] == "ef" * 16:
            raise OSError("store busy")
        return "pending"

    c, _, _ = make(state, relay, hold)
    with pytest.raises(OSError):
        c.step()
    assert relay.acked == [] and c.load()["scan"] == "" and c.load()["cursor"] is None


def test_the_sessions_own_errors_reach_the_aux_guard(state):
    relay = Relay(state)
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = Fenced("fenced")
    c, _, _ = make(state, relay, lambda i: "pending")
    with pytest.raises(Fenced):
        c.step()


@pytest.mark.parametrize(
    "page",
    [
        {"items": "x", "cursor": "AA", "more": False},
        {"items": [1], "cursor": "AA", "more": False},
        {"items": [], "cursor": "AA&cursor=x", "more": False},
        {"items": [], "cursor": "AA", "more": "no"},
        {"items": [{}] * 65, "cursor": "AA", "more": False},
    ],
)
def test_a_malformed_page_holds_nothing(state, page):
    class Bad(Relay):
        def call(self, method, path, route, **kw):
            self.calls.append((method, path, kw.get("body"), None))
            return page

    relay = Bad(state)
    held = []
    c, _, alarms = make(state, relay, lambda i: held.append(i) or "pending")
    c.step()
    assert held == [] and relay.acked == [] and alarms[0][0] == "unpublished_refused"


def test_an_unknown_hold_result_is_an_error_not_an_ack(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, _, _ = make(state, relay, lambda i: "retained")
    with pytest.raises(ValueError):
        c.step()
    assert relay.acked == []


# -- held evidence, the interim owner notice (#572) -----------------------------------------------------------


def test_held_evidence_alarms_and_is_listed_and_a_full_read_replaces_the_list(state):
    relay = Relay(state, {None: ([_it(), _it(dev="ef" * 16)], pos(2), False)})
    c, clock, alarms = make(state, relay, lambda i: "held")
    c.step()
    held = c.load()["held"]
    assert [h["device_id"] for h in held] == ["ab" * 16, "ef" * 16]
    assert alarms[-1][0] == "unpublished_held" and f"d_{'ef' * 16}" in alarms[-1][1]
    assert up.held_pairs(json.loads(state.read(up.FILE))) == held
    relay.pages[None] = ([_it()], pos(3), False)  # ef was published (retired by the relay)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert [h["device_id"] for h in c.load()["held"]] == ["ab" * 16]


def test_a_tampered_state_file_starts_over_from_the_start(state):
    state.write(up.FILE, b'{"cursor": "x&y", "held": [{"device_id": "zz"}], "gen": -1}')
    c, _, _ = make(state, Relay(state), lambda i: "pending")
    st = c.load()
    assert st["cursor"] is None and st["held"] == [] and st["gen"] is None


# -- end to end with the real store (the 11A.10 R4 adversarial list) --------------------------------------------


@pytest.fixture
def ws(tmp_path):
    env = Env(tmp_path / "w")
    env.bootstrap()
    device, sig_pub = env.add_device()
    try:
        yield env, device, sig_pub
    finally:
        env.store.close()


def _held(env):
    return [r for r in env.store._pins.revocations()]  # noqa: SLF001


def test_end_to_end_held_pending_and_every_forgery_dropped(ws, state, monkeypatch):
    env, device, sig_pub = ws
    good = item(env.owner, sig_pub, reason="lost")
    stray = item(env.owner, crypto.public_bytes(crypto.generate_private_key()))  # unassociated: pending
    sub = item(env.owner, sig_pub, reason="lost")
    sub["reason"] = "compromised"  # substituted outer field
    forged = item(env.owner, sig_pub, reason="lost")
    forged["revocation"]["o"]["reason"] = "compromised"
    other = item(env.owner, sig_pub, cert=_cert(Person(), sig_pub))  # another person's certificate
    old = item(env.owner, sig_pub, cert=_cert(env.owner, sig_pub, created_ms=(T0 - 10 * 86400) * 1000,
                                               expires_ms=(T0 - 9 * 86400) * 1000))  # fmt: skip
    results = []

    def hold(i):
        r = env.store.note_forwarded_revocation(i)
        results.append(r)
        return r

    relay = Relay(state, {None: ([sub, forged, other, good, stray, old], pos(6), False)})
    c, _, alarms = make(state, relay, hold)
    c.step()
    assert results == ["dropped", "dropped", "dropped", "held", "pending", "held"]
    assert relay.acked == [pos(6)]
    assert [h["device_id"] for h in c.load()["held"]] == [device[2:]]  # one row per pair (the store dedupes it too)
    assert any(a[0] == "unpublished_held" for a in alarms)


def test_a_cap_rejection_is_not_reported_as_held(ws, state, monkeypatch):
    env, _device, sig_pub = ws
    monkeypatch.setattr(pins_mod.HostPins, "note_revocation", lambda *a, **k: False)
    relay = Relay(state, {None: ([item(env.owner, sig_pub)], pos(1), False)})
    c, _, alarms = make(state, relay, env.store.note_forwarded_revocation)
    c.step()
    assert c.load()["held"] == [] and not any(a[0] == "unpublished_held" for a in alarms)


def test_a_crash_before_the_ack_reads_again_and_holds_idempotently(ws, state):
    env, device, sig_pub = ws
    it = item(env.owner, sig_pub)
    relay = Relay(state, {None: ([it], pos(1), False)})
    relay.fail["POST /v2/workspaces/{ws}/unpublished/ack"] = TransportError("deadline", "test")
    c, clock, _ = make(state, relay, env.store.note_forwarded_revocation)
    c.step()  # held and position durable, ack lost
    before = _held(env)
    clock.mono += up.BACKOFF_S + 1
    clock.ms += up.FULL_EVERY_MS
    c.step()  # read again from the start: the same evidence, held again idempotently, acked now
    assert _held(env) == before and relay.acked == [pos(1)]


def test_concurrent_evidence_and_appends(ws, state):
    """A device append between two pages: the second page's item is judged against the new state."""
    env, device, sig_pub = ws
    d3, sig3 = env.add_device()
    relay = Relay(
        state, {None: ([item(env.owner, sig_pub)], pos(1), True), pos(1): ([item(env.owner, sig3)], pos(2), False)}
    )
    results = []

    def hold(i):
        r = env.store.note_forwarded_revocation(i)
        results.append(r)
        return r

    c, _, _ = make(state, relay, hold)
    c.step()
    env.revoke_device(d3)  # an append lands between the pages
    c.step()
    assert results[0] == "held" and results[1] in ("held", "pending")
    assert certs.device_ref(sig3) == d3


def test_the_consumer_reaches_no_store_or_key_directly():
    """By AST: no import of orch.store, orch.custody, orch.identity or orch.crypto, and no store call: the store is
    reached only through ``hold`` (the host's worker job)."""
    import ast

    tree = ast.parse(Path(up.__file__).read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
        elif isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
    assert not any(m.startswith(("orch.store", "orch.custody", "orch.identity", "orch.crypto")) for m in mods), mods
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "note_forwarded_revocation" not in calls


@pytest.mark.parametrize("bad", ["", "a&b", "a=b", "a b", "x" * 513, None, 5])
def test_a_position_never_reaches_a_query_string_unchecked(bad):
    with pytest.raises(ValueError):
        up._position(bad)  # noqa: SLF001
    assert up._position(pos(3)) == pos(3)  # noqa: SLF001


# -- round 2: F1 (a pair the workspace already revokes), F2 (pins), F3 (bounded scan), F4-F6 --------------------


def _seam(env, state, relay):
    """The real ``relayr4.unpublished`` seam over a real store (the job runs inline)."""
    from types import SimpleNamespace

    from orch.host import relayr4

    alarms: list[tuple[str, str]] = []
    svc = SimpleNamespace(workspace_id=WS, store=env.store, clock=lambda: T0)
    worker = SimpleNamespace(session=relay, state=state, job=lambda fn: fn(), loop=None,
                             alarm=lambda code, detail: alarms.append((code, detail)))  # fmt: skip
    c = relayr4.unpublished(svc, worker)
    return c, alarms


def test_f1_a_pair_the_workspace_already_revokes_is_not_held(ws, state):
    env, device, sig_pub = ws
    it = item(env.owner, sig_pub)
    relay = Relay(state, {None: ([it], pos(1), False)})
    c, alarms = _seam(env, state, relay)
    c.step()
    assert [h["device_id"] for h in c.load()["held"]] == [device[2:]]  # not yet revoked: held, the owner is told
    env.revoke_device(device)  # the owner signs device.revoked
    st = c.load()
    st["last_full_ms"] = 0
    c._save(st)  # noqa: SLF001 - the daily full read
    n_alarms = len(alarms)
    c.step()
    st = c.load()
    assert st["held"] == [] and len(alarms) == n_alarms and relay.acked[-1] == pos(1)  # still read and acked


def test_f1_the_consumer_drops_a_published_pair_from_the_held_list(state):
    relay = Relay(state, {None: ([_it()], pos(1), True), pos(1): ([_it()], pos(2), False)})
    results = iter(["held", "published"])
    c, _, alarms = make(state, relay, lambda i: next(results))
    c.step()
    assert len(c.load()["scan_held"]) == 1
    c.step()
    assert c.load()["held"] == [] and [a[0] for a in alarms] == ["unpublished_held"]


def test_f1_the_fix_text_follows_chain_reason_and_never_asks_for_a_duplicate_revocation(state):
    from orch.host import relayr4

    plain = {**HELD_ROW, "chain_reason": None}
    chain = {**HELD_ROW, "chain_reason": "upgrade", "device_id": "ef" * 16}
    a = relayr4.held_attention("X", [plain])
    b = relayr4.held_attention("X", [chain])
    c = relayr4.held_attention("X", [plain, chain])
    assert "device.revoked event" in a["fix"] and "restore" not in a["fix"]
    assert "only a restore" in b["fix"] and "device.revoked event" not in b["fix"]
    assert "device.revoked event" in c["fix"] and "only a restore" in c["fix"]
    relay = Relay(state, {None: ([{**_it(), "chain_reason": "upgrade"}], pos(1), False)})
    cons, _, alarms = make(state, relay, lambda i: "held")
    cons.step()
    assert cons.load()["held"][0]["chain_reason"] == "upgrade" and "restore" in alarms[-1][1]
    assert up.held_pairs(json.loads(state.read(up.FILE)))[0]["chain_reason"] == "upgrade"


HELD_ROW = {"device_id": "ab" * 16, "person_id": "cd" * 16, "reason": "compromised", "digest": "sha256:" + "1" * 64,
            "at_ms": 1}  # fmt: skip


def test_f2_a_fence_error_on_the_ack_is_not_swallowed(state):  # M37 (also above)
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = Fenced("fenced")
    c, _, _ = make(state, relay, lambda i: "pending")
    with pytest.raises(Fenced):
        c.step()


def test_f2_the_notice_is_answered_only_when_an_incremental_read_completes(state):  # M16
    relay = Relay(state, {None: ([], pos(1), False), pos(1): ([], pos(2), True), pos(2): ([], pos(3), False)})
    note = {"unpublished": pos(9)}
    c, _, _ = make(state, relay, lambda i: "pending", notices=lambda: note)
    c.step()  # the full read
    note["unpublished"] = pos(10)
    relay.pages[pos(1)] = ([], pos(2), True)
    c.step()  # an incremental read from pos(1), more pages follow
    assert c.load()["notice"] == pos(9)  # not answered yet
    c.step()
    assert c.load()["notice"] == pos(10) and c.load()["cursor"] == pos(3)


def test_f2_the_position_is_checked_before_it_reaches_a_query_string(state):  # M24
    c, _, _ = make(state, Relay(state), lambda i: "pending")
    with pytest.raises(ValueError):
        c._get("x&y")  # noqa: SLF001


def test_f2_the_page_cap_and_the_deadline_are_passed_to_every_call(state):  # M09
    relay = Relay(state, {None: ([], pos(1), False)})
    c, _, _ = make(state, relay, lambda i: "pending")
    c.step()
    assert [kw["deadline_s"] for kw in relay.kws] == [up.DEADLINE_S, up.DEADLINE_S]
    assert relay.kws[0]["cap"] == up.PAGE_CAP == 4 << 20


def test_f2_the_held_list_is_bounded(state):  # M12, M34
    def page(lo):
        return [_it(dev=f"{i:032x}") for i in range(lo, lo + 64)]

    pages = {None: (page(0), pos(1), True)}
    for k in range(1, 5):
        pages[pos(k)] = (page(64 * k), pos(k + 1), k < 4)
    relay = Relay(state, pages)
    c, _, _ = make(state, relay, lambda i: "held")
    for _ in range(5):
        c.step()
    st = c.load()
    assert len(st["held"]) == up.HELD_MAX == 256  # 320 pairs held, 256 kept (the newest)
    assert st["held"][-1]["device_id"] == f"{319:032x}" and st["held"][0]["device_id"] == f"{64:032x}"
    assert len(up.held_pairs({"held": [HELD_ROW] * 300})) == up.HELD_MAX


@pytest.mark.parametrize(
    "field,bad",
    [("device_id", "zz"), ("person_id", "AB" * 16), ("reason", "x" * 33), ("reason", "\u00e9"), ("digest", "x" * 81),
     ("at_ms", "1"), ("chain_reason", 5), ("chain_reason", "x" * 33)],
)  # fmt: skip
def test_f2_a_held_row_is_checked_field_by_field(field, bad):  # M21, M22
    assert up.held_pairs({"held": [HELD_ROW]}) == [HELD_ROW]
    assert up.held_pairs({"held": [{**HELD_ROW, field: bad}]}) == []
    assert up.held_pairs({"held": [{**HELD_ROW, "extra": 1}]}) == []


class _Endless(Relay):
    """A hostile relay: every full-read page says ``more`` with a fresh cursor and a valid item."""

    def call(self, method, path, route, **kw):
        if method == "GET":
            self.calls.append((method, path, None, None))
            n = len(self.calls)
            return {"items": [_it(dev=f"{n:032x}")], "cursor": pos(n), "more": True}
        return super().call(method, path, route, **kw)


def gets(relay):
    return sum(1 for c in relay.calls if c[0] == "GET")


def test_f3_a_full_read_that_never_ends_is_cut_off_with_one_alarm_and_restarts_after_a_cooldown(state):
    relay = _Endless(state)
    holds = []
    c, clock, alarms = make(state, relay, lambda i: holds.append(1) or "held")
    for _ in range(up.SCAN_PAGES_MAX + 5):
        c.step()
    assert len(holds) <= up.SCAN_ITEMS_MAX and sum(a[0] == "unpublished_scan_unbounded" for a in alarms) == 1
    assert c.load()["scan"] == ""  # restarted from the start, not abandoned
    n = gets(relay)
    for _ in range(50):
        c.step()
    assert gets(relay) == n  # the cool-down
    clock.mono += up.SCAN_COOLDOWN_S / 2
    for _ in range(5):
        c.step()
    assert gets(relay) == n  # still inside it
    clock.mono += up.SCAN_COOLDOWN_S / 2 - 1
    clock.mono += up.SCAN_COOLDOWN_S + 1
    c.step()
    assert gets(relay) == n + 1
    assert [c for c in relay.calls if c[0] == "GET"][-1][1].endswith("unpublished?limit=64")


def test_f3_a_page_that_repeats_an_item_costs_one_job(state):
    relay = Relay(state, {None: ([_it()] * 64, pos(1), False)})
    holds = []
    c, _, alarms = make(state, relay, lambda i: holds.append(1) or "held")
    c.step()
    assert len(holds) == 1 and sum(a[0] == "unpublished_held" for a in alarms) == 1


def test_f4_a_store_fault_or_an_unknown_result_backs_off(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})

    def boom(i):
        raise RuntimeError("store")

    c, clock, _ = make(state, relay, boom)
    with pytest.raises(RuntimeError):
        c.step()
    n = len(relay.calls)
    c.step()
    c.step()
    assert len(relay.calls) == n  # no new GET until the back-off passed
    clock.mono += up.BACKOFF_S + 1
    with pytest.raises(RuntimeError):
        c.step()
    assert len(relay.calls) == n + 1


def test_f5_a_clock_set_back_does_not_suppress_the_daily_read(state):
    relay = Relay(state)
    c, clock, _ = make(state, relay, lambda i: "pending")
    clock.ms += 10 * 86400 * 1000  # the clock was far ahead
    c.step()
    assert c.load()["last_full_ms"] == clock.ms
    clock.ms -= 10 * 86400 * 1000  # and is corrected
    n = gets(relay)
    c.step()
    assert gets(relay) == n + 1  # stored time in the future: read again
    # the process's own monotonic clock also brings it back, whatever the wall clock says
    n = gets(relay)
    clock.mono += up.FULL_EVERY_S + 1
    clock.ms = c.load()["last_full_ms"]  # a wall clock that stands still
    c.step()
    assert gets(relay) == n + 1


def test_f6_a_page_is_held_within_a_time_budget_and_the_rest_carries_over_without_an_ack(state):
    items = [_it(dev=f"{i:032x}") for i in range(10)]
    relay = Relay(state, {None: (items, pos(1), False)})
    held = []
    clock = Clock()

    def hold(i):
        held.append(i["device_id"])
        clock.mono += up.HOLD_BUDGET_S / 3  # a slow worker queue
        return "pending"

    c, _, _ = make(state, relay, hold, clock=clock)
    c.step()
    assert 1 <= len(held) < 10 and relay.acked == []  # stopped at the budget: no ack, position not moved
    assert c.load()["resume"]["n"] == len(held) and c.load()["cursor"] is None
    for _ in range(20):
        c.step()
        if relay.acked:
            break
    assert relay.acked == [pos(1)] and held == [f"{i:032x}" for i in range(10)]  # each held exactly once
    assert c.load()["resume"] is None


def test_f6_a_changed_page_is_held_again_from_its_start(state):
    a = [_it(dev=f"{i:032x}") for i in range(6)]
    relay = Relay(state, {None: (a, pos(1), False)})
    held = []
    clock = Clock()

    def hold(i):
        held.append(i["device_id"])
        clock.mono += up.HOLD_BUDGET_S / 2
        return "pending"

    c, _, _ = make(state, relay, hold, clock=clock)
    c.step()
    assert c.load()["resume"] is not None
    relay.pages[None] = ([_it(dev=f"{i:032x}") for i in range(10, 16)], pos(1), False)  # the relay answers otherwise
    held.clear()
    c.step()
    assert held[0] == f"{10:032x}"  # the first item of the new page is not skipped


class _Wide(Relay):
    """A hostile relay: pages of 64 distinct items, ``more`` forever."""

    def call(self, method, path, route, **kw):
        if method == "GET":
            self.calls.append((method, path, None, None))
            n = sum(1 for c in self.calls if c[0] == "GET")
            return {"items": [_it(dev=f"{n * 64 + i:032x}") for i in range(64)], "cursor": pos(n), "more": True}
        return super().call(method, path, route, **kw)


def test_f3_the_item_bound_cuts_off_wide_pages_before_the_page_bound(state):
    relay = _Wide(state)
    holds = []
    c, _, alarms = make(state, relay, lambda i: holds.append(1) or "pending")
    for _ in range(up.SCAN_PAGES_MAX):
        c.step()
    assert len(holds) <= up.SCAN_ITEMS_MAX and sum(a[0] == "unpublished_scan_unbounded" for a in alarms) == 1
    assert gets(relay) <= up.SCAN_ITEMS_MAX // 64 + 1


def test_f6_a_zero_budget_still_holds_one_item_per_turn(state, monkeypatch):
    monkeypatch.setattr(up, "HOLD_BUDGET_S", 0.0)
    items = [_it(dev=f"{i:032x}") for i in range(4)]
    relay = Relay(state, {None: (items, pos(1), False)})
    held = []
    c, _, _ = make(state, relay, lambda i: held.append(i["device_id"]) or "pending")
    for _ in range(6):
        c.step()
    assert relay.acked == [pos(1)] and len(held) == 4
