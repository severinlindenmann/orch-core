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
    assert results[0] == "held" and results[1] in ("held", "pending", "settled")
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


def test_f2_the_position_is_checked_before_it_reaches_a_query_string(state):  # M24 / O24
    relay = Relay(state)
    c, _, _ = make(state, relay, lambda i: "pending")
    with pytest.raises(ValueError):
        c._get("x&y")  # noqa: SLF001
    assert relay.calls == []  # refused by the consumer's own check, before any request


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


def test_f6_a_zero_budget_still_holds_one_item_per_turn(state, monkeypatch):
    monkeypatch.setattr(up, "HOLD_BUDGET_S", 0.0)
    items = [_it(dev=f"{i:032x}") for i in range(4)]
    relay = Relay(state, {None: (items, pos(1), False)})
    held = []
    c, _, _ = make(state, relay, lambda i: held.append(i["device_id"]) or "pending")
    for _ in range(6):
        c.step()
    assert relay.acked == [pos(1)] and len(held) == 4


def gets(relay):
    return sum(1 for c in relay.calls if c[0] == "GET")


# -- round 3: the invariant (settle), strength, sanitising, continuation -------------------------------------------


def _row(dev, weaker=False):
    return {**HELD_ROW, "device_id": dev, "weaker": weaker}


def _res(*results):
    it = iter(results)
    return lambda i: next(it)


def test_d1_only_a_settled_answer_takes_a_pair_out_of_view(state):
    a, b = "aa" * 16, "bb" * 16
    relay = Relay(state, {None: ([_it(dev=a), _it(dev=b)], pos(1), False)})
    c, clock, alarms = make(state, relay, _res("held", "weaker"))
    c.step()
    st = c.load()
    assert [h["device_id"] for h in st["held"]] == [a, b] and [h["weaker"] for h in st["held"]] == [False, True]
    n = len(alarms)
    # a later read: A settles, B does not: only A leaves (N08), and nothing but "settled" removes anything (N05)
    for results in (("pending", "dropped"), ("settled", "weaker")):
        clock.ms += up.FULL_EVERY_MS
        clock.mono += up.BACKOFF_S + 1
        c._hold = _res(*results)  # noqa: SLF001
        c.step()
    held = c.load()["held"]
    assert [h["device_id"] for h in held] == [b] and alarms[n:] == [alarms[-1]]


def test_d1_pending_and_dropped_never_remove_a_held_row(state):
    a = "aa" * 16
    relay = Relay(state, {None: ([_it(dev=a)], pos(1), False)})
    c, clock, _ = make(state, relay, _res("held"))
    c.step()
    for result in ("pending", "dropped"):
        c._hold = _res(result)  # noqa: SLF001
        st = c.load()
        st["notice"] = None
        c._save(st)  # noqa: SLF001
        relay.pages[None] = ([_it(dev=a)], pos(1), False)
        # an incremental read of the same pair: no scan, so no list rebuild: the row must still be there
        note = {"unpublished": pos(7 + len(result))}
        c._notices = lambda note=note: note  # noqa: SLF001
        c.step()
        assert [h["device_id"] for h in c.load()["held"]] == [a]


def test_d1_an_unknown_answer_is_an_error_and_never_settles(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, _, _ = make(state, relay, lambda i: "published")
    with pytest.raises(ValueError):
        c.step()
    assert relay.acked == []


@pytest.mark.parametrize(
    "chain,item_reason,want",
    [(None, "compromised", "held"), (None, "lost", "held"), ("lost", "compromised", "weaker"),
     ("retired", "compromised", "weaker"), ("lost", "lost", "settled"), ("lost", "retired", "settled"),
     ("retired", "lost", "settled"), ("compromised", "compromised", "settled"), ("compromised", "lost", "settled"),
     ("compromised", "retired", "settled")],
)  # fmt: skip
def test_d1_the_store_settles_only_at_least_as_strong_a_revocation_of_the_exact_pair(ws, chain, item_reason, want):
    env, device, sig_pub = ws
    if chain is not None:
        env.revoke_device(device, reason=chain)
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub, reason=item_reason)) == want


def test_d1_a_revocation_of_another_device_of_the_person_does_not_settle(ws):  # N01
    env, device, sig_pub = ws
    d2, _sig2 = env.add_device()
    env.revoke_device(d2, reason="compromised")
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub)) == "held"


def test_d1_without_host_state_nothing_is_settled(ws):  # N02
    env, device, sig_pub = ws
    env.revoke_device(device, reason="compromised")
    env.store._state = None  # noqa: SLF001
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub)) == "dropped"  # never "settled"


def test_d1_through_the_real_seam_a_weaker_chain_pair_stays_visible(ws, state):
    from orch.host import relayr4

    env, device, sig_pub = ws
    env.revoke_device(device, reason="lost")
    relay = Relay(state, {None: ([item(env.owner, sig_pub, reason="compromised")], pos(1), False)})
    c, alarms = _seam(env, state, relay)
    c.step()
    (row,) = c.load()["held"]
    assert row["weaker"] is True and row["reason"] == "compromised" and alarms[-1][0] == "unpublished_held"
    assert "stronger reason compromised" in alarms[-1][1]
    note = relayr4.held_attention("X", up.held_pairs(json.loads(state.read(up.FILE))))
    assert "stronger" in note["fix"] and "device.revoked" in note["fix"] and "harmless" in note["fix"]
    env.revoke_device(device, reason="compromised")  # the owner upgrades: it lands, the next read settles it
    clock_ms = c.load()
    clock_ms["last_full_ms"] = 0
    c._save(clock_ms)  # noqa: SLF001
    c.step()
    assert c.load()["held"] == []


def test_d3_the_fix_text_follows_the_store_never_the_relay(state):
    from orch.host import relayr4

    plain, weak = {**HELD_ROW, "weaker": False}, {**HELD_ROW, "device_id": "ef" * 16, "weaker": True}
    a, b, both = (relayr4.held_attention("X", p) for p in ([plain], [weak], [plain, weak]))
    assert a["fix"] == relayr4.FIX_REVOKE and b["fix"] == relayr4.FIX_STRONGER
    assert relayr4.FIX_REVOKE in both["fix"] and relayr4.FIX_STRONGER in both["fix"]
    for text in (a["fix"], b["fix"]):
        assert "re-append" not in text and "again" not in text and "carry" not in text
    # a hostile outer chain_reason does nothing at all
    relay = Relay(state, {None: ([{**_it(), "chain_reason": "upgrade"}], pos(1), False)})
    cons, _, alarms = make(state, relay, lambda i: "held")
    cons.step()
    assert "weaker" in cons.load()["held"][0] and cons.load()["held"][0]["weaker"] is False
    assert "chain_reason" not in cons.load()["held"][0] and "restore" not in alarms[-1][1]


HOSTILE = ["\n", "\x1b[2J", 5, None, "x" * 500, "\u00e9", "a&b", "", ["x"], {"k": 1}, b"\x00"]


@pytest.mark.parametrize("bad", HOSTILE)
@pytest.mark.parametrize("field", ["device_id", "person_id", "reason", "digest", "chain_reason"])
def test_d2_hostile_fields_never_hide_a_held_notice(state, field, bad):  # N26
    it = _it()
    if field == "digest":
        it["provenance"] = {"cursor": 1, "digest": bad}
    else:
        it[field] = bad
    relay = Relay(state, {None: ([it], pos(1), False)})
    c, _, alarms = make(state, relay, lambda i: "held")
    c.step()
    st = json.loads(state.read(up.FILE))
    (row,) = up.held_pairs(st)  # a row exists and survives the status reader's validation
    assert alarms[-1][0] == "unpublished_held" and relay.acked == [pos(1)]
    assert row["reason"] in ("compromised", "lost", "retired", "unknown")
    assert alarms[-1][1].isprintable()
    if field == "reason":
        assert row["reason"] == "unknown"


def test_d4_a_long_scan_continues_from_its_position_and_alarms_once(state, monkeypatch):
    monkeypatch.setattr(up, "SCAN_LONG_PAGES", 3)
    total = 12
    pages = {None: ([_it(dev=f"{1:032x}")], pos(1), True)}
    for k in range(1, total):
        pages[pos(k)] = ([_it(dev=f"{k + 1:032x}")], pos(k + 1), k < total - 1)
    relay = Relay(state, pages)
    seen = []
    c, clock, alarms = make(state, relay, lambda i: seen.append(i["device_id"]) or "pending")
    for _ in range(3):
        c.step()
    assert not any(a[0] == "unpublished_scan_long" for a in alarms)  # exactly at the bound: not yet (N13)
    c.step()
    assert sum(a[0] == "unpublished_scan_long" for a in alarms) == 1
    n = gets(relay)
    c.step()
    assert gets(relay) == n  # slowed: one page per back-off
    for _ in range(40):
        clock.mono += up.BACKOFF_S + 1
        c.step()
    assert seen == [f"{k:032x}" for k in range(1, total + 1)]  # every page reached, in order, each once
    assert sum(a[0] == "unpublished_scan_long" for a in alarms) == 1
    assert c.load()["scan"] is None and c.load()["scan_pages"] == 0  # a completed read resets the counter (N29)


def test_d4_a_scan_is_never_restarted_by_its_length(state):
    pages = {None: ([], pos(1), True)}
    for k in range(1, 200):
        pages[pos(k)] = ([], pos(k + 1), k < 199)
    relay = Relay(state, pages)
    c, clock, _ = make(state, relay, lambda i: "pending")
    for _ in range(230):
        clock.mono += up.BACKOFF_S + 1
        c.step()
    gets_from_start = [c_ for c_ in relay.calls if c_[0] == "GET" and c_[1].endswith("unpublished?limit=64")]
    assert len(gets_from_start) == 1 and c.load()["scan"] is None and c.load()["cursor"] == pos(200)


def test_d5_a_permanently_refused_ack_is_dropped_after_five_refusals_with_one_alarm(state):
    relay = Relay(state, {None: ([], pos(1), True), pos(1): ([], pos(2), False)})
    c, clock, alarms = make(state, relay, lambda i: "pending")
    for _ in range(up.ACK_REFUSALS_MAX):
        relay.fail[ACK] = RelayRefused(400, "bad_request")
        c.step()
        clock.mono += up.BACKOFF_S + 1
    assert c.load()["ack"] is None and sum(a[0] == "unpublished_ack_dropped" for a in alarms) == 1
    c.step()  # the scan goes on from its stored position
    assert relay.calls[-2][1].endswith(f"cursor={pos(1)}&limit=64") or relay.acked == [pos(2)]


def test_d5_rate_limits_and_transport_errors_never_drop_an_ack(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    c, clock, _ = make(state, relay, lambda i: "pending")
    for k in range(up.ACK_REFUSALS_MAX + 3):
        relay.fail[ACK] = RelayRefused(429, "rate_limited", retry_after=1) if k < 6 else TransportError("d", "t")
        c.step()
        clock.mono += up.BACKOFF_S + 1
    assert c.load()["ack"] == pos(1)


def test_d5_four_refusals_then_success_keep_the_ack_and_reset_the_count(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    c, clock, _ = make(state, relay, lambda i: "pending")
    for _ in range(up.ACK_REFUSALS_MAX - 1):
        relay.fail[ACK] = RelayRefused(400, "bad_request")
        c.step()
        clock.mono += up.BACKOFF_S + 1
    assert c.load()["ack"] == pos(1)
    c.step()
    assert relay.acked == [pos(1)] and c.load()["ack"] is None and c.load()["ack_refusals"] == 0


# -- structure ----------------------------------------------------------------------------------------------------


def _fn_sources():
    import ast
    import inspect

    src = inspect.getsource(up)
    tree = ast.parse(src)
    return tree, {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def test_structure_the_held_view_changes_in_one_place():
    import ast

    tree, fns = _fn_sources()
    # a held row is never skipped: _held has no return statement at all (it always creates the row and alarms)
    assert not [n for n in ast.walk(fns["_held"]) if isinstance(n, ast.Return)]
    # the only code that filters a row out of a held list is _settle (and _held replaces its own pair's row)
    removers = set()
    for name, fn in fns.items():
        for n in ast.walk(fn):
            if isinstance(n, ast.ListComp) and "!=" in ast.unparse(n):
                removers.add(name)
    assert removers == {"_settle", "_held"}
    assert "_unheld" not in fns and "pair_revoked" not in ast.unparse(tree)
    # "settled" appears only in _settle
    users = {name for name, fn in fns.items() if "'settled'" in ast.unparse(fn)}
    assert users == {"_settle"}


def test_structure_one_reason_strength_table():
    import ast

    root = Path(up.__file__).resolve().parents[1]
    found = []
    for path in root.rglob("*.py"):
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.Dict):
                keys = {k.value for k in n.keys if isinstance(k, ast.Constant)}
                if {"compromised", "lost", "retired"} <= keys and all(
                    isinstance(v, ast.Constant) and type(v.value) is int for v in n.values
                ):
                    found.append(path.relative_to(root).as_posix())
    assert found == ["identity/certs.py"]


def test_the_models_upgrade_rule_agrees_with_revoke_strength(ws):
    env, _d, _s = ws
    for prev in certs.REVOKE_REASONS:
        for new in certs.REVOKE_REASONS:
            device, _sig = env.add_device()
            env.revoke_device(device, reason=prev)
            try:
                env.revoke_device(device, reason=new)
            except Exception:  # noqa: BLE001 - a refused append
                pass
            dev_person = env.store._state.revoked_reason(device, "p_" + crypto.person_id(env.owner.pk_pub).hex())  # noqa: SLF001
            want = new if certs.revoke_strength(new) > certs.revoke_strength(prev) else prev
            assert dev_person == want, (prev, new)
    with pytest.raises(KeyError):
        certs.revoke_strength("unknown")


def test_d1_a_pair_held_earlier_in_the_same_scan_is_settled_out_of_the_scan_list_too(state):
    a = "aa" * 16
    relay = Relay(state, {None: ([_it(dev=a)], pos(1), True), pos(1): ([_it(dev=a)], pos(2), False)})
    c, _, _ = make(state, relay, _res("held", "settled"))
    c.step()
    assert [h["device_id"] for h in c.load()["scan_held"]] == [a]
    c.step()  # the pair settles while the scan is still collecting: it must not come back at the end of the scan
    assert c.load()["held"] == [] and c.load()["scan_held"] == []
