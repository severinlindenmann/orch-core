"""R4: the unpublished-evidence consumer and its held LEDGER (11A.9, relay protocol §19.5 P3-D31).

A scripted session plays the relay; a fake store (the contract the consumer sees) drives every cell of the 11A.9
ledger table; the real store holds the evidence for the end-to-end and adversarial cases; a model-based test checks the
ledger against an oracle over random sequences of relay inputs, restarts, ledger losses, store commits and crashes.
The invariant: relay input only adds or raises rows; only the store's own verified state removes one; the ledger
never loses an obligation the store has not settled."""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import pytest

from orch import canon, crypto
from orch.host import relayr4
from orch.identity import certs
from orch.relay import unpublished as up
from orch.relay.http import TransportError
from orch.relay.retry import RelayRefused
from orch.relay.session import Fenced
from orch.relay.state import StateDir
from tests.identity.helpers import Person
from tests.store.helpers import T0, Env
from tests.store.test_forwarded_revocation import _cert, item

WS = "705d40abbb8c1c90354a1acaa94c935c"
GET = "GET /v2/workspaces/{ws}/unpublished"
ACK = "POST /v2/workspaces/{ws}/unpublished/ack"
A, B, C = "aa" * 16, "bb" * 16, "cc" * 16
P1, P2 = "cd" * 16, "ef" * 16


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


class FakeStore:
    """The store's contract as the consumer sees it (``Store.note_forwarded_revocation`` / ``pair_standings``): the
    workspace state is ``state[(d_, p_)] = reason``; ``kind[(d_, p_)]`` says what an unrevoked pair is
    (``revoke`` | ``other`` | ``unknown``)."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str], str] = {}
        self.kind: dict[tuple[str, str], str] = {}
        self.delivered: dict[tuple[str, str], str] = {}  # the oracle: pairs the store judged held, strongest reason
        self.raise_at: int | None = None
        self.calls = 0

    @staticmethod
    def pair(item_):
        return "d_" + item_["device_id"], "p_" + item_["person_id"]

    def standing(self, d, p, reason):
        have = self.state.get((d, p))
        if have is not None:
            return "settled" if certs.revoke_strength(have) >= certs.revoke_strength(reason) else "stronger"
        return self.kind.get((d, p), "revoke")

    def hold(self, item_):
        self.calls += 1
        if self.raise_at is not None and self.calls == self.raise_at:
            raise RuntimeError("store fault")
        if item_.get("bad"):
            return "unverifiable"
        if item_.get("stranger"):
            return "dropped"
        d, p = self.pair(item_)
        st = self.standing(d, p, item_["reason"])
        if st == "settled":
            return "settled"
        cur = self.delivered.get((d, p))
        if cur is None or relayr4.stronger(item_["reason"], cur):
            self.delivered[(d, p)] = item_["reason"]
        return "held"

    def judge(self, pairs):
        return [self.standing(d, p, r) for d, p, r in pairs]

    def commit(self, d, p, reason):
        """A revocation lands in the workspace state; the model only lets a stronger one replace a weaker one."""
        have = self.state.get((d, p))
        if have is None or certs.revoke_strength(reason) > certs.revoke_strength(have):
            self.state[(d, p)] = reason


@pytest.fixture
def state(tmp_path):
    s = StateDir(tmp_path / "relay")
    yield s
    s.close()


def make(state, relay, hold, *, notices=None, clock=None, alarms=None, judge=None, evidence=None):
    clock = clock or Clock()
    alarms = alarms if alarms is not None else []
    c = up.UnpublishedConsumer(
        workspace_id=WS, session=relay, state=state, hold=hold, stronger=relayr4.stronger, judge=judge,
        evidence=evidence, notices=lambda: notices() if notices else None,
        alarm=lambda code, detail: alarms.append((code, detail)), now_ms=clock.now, mono=lambda: clock.mono,
    )  # fmt: skip
    return c, clock, alarms


def fake(state, relay, *, notices=None, evidence=None):
    """A consumer over a FakeStore: ``(consumer, store, clock, alarms)``."""
    st = FakeStore()
    c, clock, alarms = make(state, relay, st.hold, notices=notices, judge=st.judge, evidence=evidence)
    return c, st, clock, alarms


def _it(dev=A, person=P1, reason="compromised", digest="sha256:" + "1" * 64, **extra):
    return {
        "device_id": dev,
        "person_id": person,
        "reason": reason,
        "provenance": {"cursor": 1, "digest": digest},
        **extra,
    }


def gets(relay):
    return sum(1 for c in relay.calls if c[0] == "GET")


def rows(c):
    return {(r["device_id"], r["person_id"]): r for r in c.load()[0]["held"]}


def tick(c, clock, n=1):
    for _ in range(n):
        clock.mono += up.BACKOFF_S + 1
        c.step()


def _res(*results):
    it = iter(results)
    return lambda i: next(it)


# -- when and what it reads -----------------------------------------------------------------------------------------


def test_a_new_holder_generation_reads_from_the_start_page_by_page_then_acks_each(state):
    relay = Relay(state, {None: ([], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, _, _ = fake(state, relay)
    gets_ = []
    orig = relay.call
    relay.call = lambda m, path, *a, **k: gets_.append(path) or orig(m, path, *a, **k)
    c.step()
    c.step()
    assert relay.acked == [pos(1), pos(2)] and sum(1 for g in gets_ if "unpublished?" in g) == 2


def test_the_position_is_durable_before_each_ack_and_every_item_judged_before(state):
    relay = Relay(state, {None: ([_it(), _it(dev=B)], pos(2), False)})
    c, _, _, _ = fake(state, relay)
    c.step()
    ack = next(call for call in relay.calls if call[0] == "POST")
    assert ack[2] == {"cursor": pos(2)} and ack[3]["cursor"] == pos(2) and ack[3]["ack"] == pos(2)
    assert {r["device_id"] for r in ack[3]["held"]} == {A, B}  # the ledger is on disk before the ack


def test_nothing_is_read_without_a_holder_generation(state):
    relay = Relay(state, gen=None)
    relay.gen = None
    relay.call = lambda *a, **k: pytest.fail("no call without a generation")
    c, _, _, _ = fake(state, relay)
    c.step()


def test_after_the_full_read_only_a_new_notice_triggers_a_read(state):
    relay = Relay(state)
    note = {"unpublished": pos(7)}
    c, _, _, _ = fake(state, relay, notices=lambda: note)
    c.step()
    n = gets(relay)
    c.step()
    assert gets(relay) == n  # the same notice is answered
    note["unpublished"] = pos(8)
    c.step()
    assert gets(relay) == n + 1
    note["unpublished"] = None
    c.step()
    assert gets(relay) == n + 1


def test_the_notice_is_answered_only_when_an_incremental_read_completes(state):  # M16
    relay = Relay(state, {None: ([], pos(1), False), pos(1): ([], pos(2), True), pos(2): ([], pos(3), False)})
    note = {"unpublished": pos(9)}
    c, _, _, _ = fake(state, relay, notices=lambda: note)
    c.step()
    note["unpublished"] = pos(10)
    c.step()
    assert c.load()[0]["notice"] == pos(9)  # not answered yet
    c.step()
    assert c.load()[0]["notice"] == pos(10) and c.load()[0]["cursor"] == pos(3)


def test_a_daily_full_read_and_a_new_generation_restart_from_the_start(state):
    relay = Relay(state)
    c, _, clock, _ = fake(state, relay)
    c.step()
    n = len(relay.calls)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")
    n = len(relay.calls)
    relay.gen = 2
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64") and c.load()[0]["gen"] == 2


def test_a_new_generation_restarts_the_scan_but_keeps_the_ledger(state):
    relay = Relay(state, {None: ([_it()], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, _, _ = fake(state, relay)
    c.step()
    mid = c.load()[0]
    assert mid["scan"] == pos(1) and mid["gen"] == 1 and (A, P1) in rows(c)
    relay.gen = 2  # the holder crashed; a NEW generation takes over
    n = len(relay.calls)
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")  # from the start, not from pos(1)
    assert c.load()[0]["gen"] == 2 and (A, P1) in rows(c)  # the ledger is never reset by a generation change


def test_the_same_generation_continues_its_scan(state):
    relay = Relay(state, {None: ([], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, _, _ = fake(state, relay)
    c.step()
    n = len(relay.calls)
    c.step()
    assert relay.calls[n][1].endswith(f"cursor={pos(1)}&limit=64")


# -- the owed ack ----------------------------------------------------------------------------------------------------


def test_a_failed_ack_is_persisted_first_and_retried_after_backoff(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = TransportError("deadline", "test")
    c, _, clock, _ = fake(state, relay)
    c.step()
    post = next(call for call in relay.calls if call[0] == "POST")
    assert post[3]["ack"] == pos(1) and c.load()[0]["ack"] == pos(1) and relay.acked == []
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n  # backoff: nothing sent yet
    clock.mono += up.BACKOFF_S + 1
    c.step()
    assert relay.acked == [pos(1)] and c.load()[0]["ack"] is None
    assert [call[0] for call in relay.calls[n:]] == ["POST"]  # the retry is the ack, not a new read


def test_a_pending_ack_is_retried_after_a_restart(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = RelayRefused(503, "unavailable")
    c, _, _, _ = fake(state, relay)
    c.step()
    assert relay.acked == [] and c.load()[0]["ack"] == pos(1)
    c2, _, _, _ = fake(state, relay)  # a new process: no in-memory backoff
    n = len(relay.calls)
    c2.step()
    assert relay.acked == [pos(1)] and c2.load()[0]["ack"] is None
    assert [call[0] for call in relay.calls[n:]] == ["POST"]


def test_a_pending_ack_is_not_cleared_until_the_relay_accepted_it(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = RelayRefused(429, "rate_limited", retry_after=30)
    c, _, clock, alarms = fake(state, relay)
    c.step()
    assert c.load()[0]["ack"] == pos(1) and alarms[0][0] == "unpublished_rate_limited"
    clock.mono += 29
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n and c.load()[0]["ack"] == pos(1)


def test_a_fence_error_on_the_ack_stops_the_worker_and_keeps_the_ack_owed(state):  # M37
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = Fenced("fenced")
    c, _, _, _ = fake(state, relay)
    with pytest.raises(Fenced):
        c.step()
    assert c.load()[0]["ack"] == pos(1)


def test_a_pending_ack_of_an_older_generation_is_dropped_and_the_read_restarts(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = TransportError("deadline", "test")
    c, _, clock, _ = fake(state, relay)
    c.step()
    assert c.load()[0]["ack"] == pos(1)
    relay.gen = 2
    clock.mono += up.BACKOFF_S + 1
    n = len(relay.calls)
    c.step()
    assert [call[0] for call in relay.calls[n:]] == ["GET", "POST"]
    assert relay.calls[n][1].endswith("unpublished?limit=64") and c.load()[0]["ack"] is None


def test_cursor_ahead_on_the_ack_drops_it_and_restarts(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    relay.fail[ACK] = RelayRefused(409, "cursor_ahead")
    c, _, _, _ = fake(state, relay)
    c.step()
    st = c.load()[0]
    assert st["ack"] is None and st["scan"] == ""


def test_a_permanently_refused_ack_is_dropped_after_five_refusals_with_one_alarm(state):
    relay = Relay(state, {None: ([], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, clock, alarms = fake(state, relay)
    for _ in range(up.ACK_REFUSALS_MAX):
        relay.fail[ACK] = RelayRefused(400, "bad_request")
        c.step()
        clock.mono += up.BACKOFF_S + 1
    assert c.load()[0]["ack"] is None and sum(a[0] == "unpublished_ack_dropped" for a in alarms) == 1
    n = gets(relay)
    c.step()  # the scan goes on from its stored position
    assert gets(relay) == n + 1


def test_rate_limits_and_transport_errors_never_drop_an_ack(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    c, _, clock, _ = fake(state, relay)
    for k in range(up.ACK_REFUSALS_MAX + 3):
        relay.fail[ACK] = RelayRefused(429, "rate_limited", retry_after=1) if k < 6 else TransportError("d", "t")
        c.step()
        clock.mono += up.BACKOFF_S + 1
    assert c.load()[0]["ack"] == pos(1)


def test_four_refusals_then_success_keep_the_ack_and_reset_the_count(state):
    relay = Relay(state, {None: ([], pos(1), False)})
    c, _, clock, _ = fake(state, relay)
    for _ in range(up.ACK_REFUSALS_MAX - 1):
        relay.fail[ACK] = RelayRefused(400, "bad_request")
        c.step()
        clock.mono += up.BACKOFF_S + 1
    assert c.load()[0]["ack"] == pos(1)
    c.step()
    assert relay.acked == [pos(1)] and c.load()[0]["ack"] is None and c.load()[0]["ack_refusals"] == 0


# -- failures --------------------------------------------------------------------------------------------------------


def test_cursor_ahead_restarts_from_the_start_with_a_backoff_and_keeps_the_ledger(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    note = {"unpublished": pos(9)}
    c, _, clock, alarms = fake(state, relay, notices=lambda: note)
    c.step()
    relay.fail[GET] = RelayRefused(409, "cursor_ahead")
    note["unpublished"] = pos(10)
    c.step()
    assert c.load()[0]["scan"] == "" and alarms[-1][0] == "unpublished_cursor_ahead" and (A, P1) in rows(c)
    n = len(relay.calls)
    c.step()
    assert len(relay.calls) == n  # F4: backed off, the relay's answer does not set the pace
    clock.mono += up.BACKOFF_S + 1
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")


def test_rate_limited_waits_for_retry_after(state):
    relay = Relay(state)
    relay.fail[GET] = RelayRefused(429, "rate_limited", retry_after=120)
    c, _, clock, alarms = fake(state, relay)
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
    relay.fail[GET] = TransportError("deadline", "test")
    c, _, clock, alarms = fake(state, relay)
    c.step()
    clock.mono += up.BACKOFF_S + 1
    relay.fail[GET] = TransportError("deadline", "test")
    c.step()
    assert [a[0] for a in alarms] == ["unpublished_transport"]


def test_the_sessions_own_errors_reach_the_aux_guard(state):
    relay = Relay(state)
    relay.fail[GET] = Fenced("fenced")
    c, _, _, _ = fake(state, relay)
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
def test_a_malformed_page_judges_nothing_and_backs_off(state, page):
    relay = Relay(state)
    relay.call = lambda m, path, route, **kw: page if m == "GET" else pytest.fail("no ack")
    held = []
    c, _, alarms = make(state, relay, lambda i: held.append(i) or "held")
    c.step()
    assert held == [] and alarms[0][0] == "unpublished_refused"


def test_an_unknown_hold_result_is_an_error_not_an_ack(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, _, _ = make(state, relay, lambda i: "published")
    with pytest.raises(ValueError):
        c.step()
    assert relay.acked == []


def test_a_store_fault_keeps_the_items_before_it_and_acks_nothing(state):  # H3
    relay = Relay(state, {None: ([_it(dev=A), _it(dev=B), _it(dev=C)], pos(3), False)})
    c, st, clock, _ = fake(state, relay)
    st.raise_at = 3  # the third item's job fails
    with pytest.raises(RuntimeError):
        c.step()
    assert relay.acked == [] and set(d for d, _p in rows(c)) == {A, B}  # items 1..2 are already persisted
    st.raise_at = None
    n = gets(relay)
    c.step()
    assert gets(relay) == n  # backed off
    tick(c, clock)
    assert relay.acked == [pos(3)] and set(d for d, _p in rows(c)) == {A, B, C}


def test_a_store_fault_in_the_judging_job_removes_nothing_and_backs_off(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    c.step()
    st.state[("d_" + A, "p_" + P1)] = "compromised"  # settled in the store ...
    st.judge = lambda pairs: (_ for _ in ()).throw(RuntimeError("judge fault"))  # ... but the job fails
    c._judge = lambda pairs: st.judge(pairs)  # noqa: SLF001
    clock.mono += up.JUDGE_EVERY_S + 1
    with pytest.raises(RuntimeError):
        c.step()
    assert (A, P1) in rows(c)  # no answer: nothing removed


@pytest.mark.parametrize("bad", ["", "a&b", "a=b", "a b", "x" * 513, None, 5])
def test_a_position_never_reaches_a_query_string_unchecked(bad):
    with pytest.raises(ValueError):
        up._position(bad)  # noqa: SLF001


def test_the_position_is_checked_before_it_reaches_a_query_string(state):  # M24
    relay = Relay(state)
    c, _, _, _ = fake(state, relay)
    with pytest.raises(ValueError):
        c._get("x&y")  # noqa: SLF001
    assert relay.calls == []


def test_the_page_cap_and_the_deadline_are_passed_to_every_call(state):  # M09
    relay = Relay(state, {None: ([], pos(1), False)})
    c, _, _, _ = fake(state, relay)
    c.step()
    assert [kw["deadline_s"] for kw in relay.kws] == [up.DEADLINE_S, up.DEADLINE_S]
    assert relay.kws[0]["cap"] == up.PAGE_CAP == 4 << 20


def test_a_page_that_repeats_an_item_costs_one_job(state):
    relay = Relay(state, {None: ([_it()] * 64, pos(1), False)})
    held = []
    c, _, alarms = make(state, relay, lambda i: held.append(1) or "held")
    c.step()
    assert len(held) == 1 and sum(a[0] == "unpublished_held" for a in alarms) == 1


def test_a_clock_set_back_does_not_suppress_the_daily_read(state):
    relay = Relay(state)
    c, _, clock, _ = fake(state, relay)
    clock.ms += 10 * 86400 * 1000
    c.step()
    assert c.load()[0]["last_full_ms"] == clock.ms
    clock.ms -= 10 * 86400 * 1000
    n = gets(relay)
    c.step()
    assert gets(relay) == n + 1  # stored time in the future: read again
    n = gets(relay)
    clock.mono += up.FULL_EVERY_S + 1
    clock.ms = c.load()[0]["last_full_ms"]
    c.step()
    assert gets(relay) == n + 1


def test_a_page_is_judged_within_a_time_budget_and_the_rest_carries_over_without_an_ack(state):
    items = [_it(dev=f"{i:032x}") for i in range(10)]
    relay = Relay(state, {None: (items, pos(1), False)})
    seen = []
    clock = Clock()

    def hold(i):
        seen.append(i["device_id"])
        clock.mono += up.HOLD_BUDGET_S / 3
        return "settled"

    c, _, _ = make(state, relay, hold, clock=clock)
    c.step()
    assert 1 <= len(seen) < 10 and relay.acked == []
    assert c.load()[0]["resume"]["n"] == len(seen) and c.load()[0]["cursor"] is None
    for _ in range(20):
        c.step()
        if relay.acked:
            break
    assert relay.acked == [pos(1)] and seen == [f"{i:032x}" for i in range(10)]
    assert c.load()[0]["resume"] is None


def test_a_changed_page_is_judged_again_from_its_start(state):
    relay = Relay(state, {None: ([_it(dev=f"{i:032x}") for i in range(6)], pos(1), False)})
    seen = []
    clock = Clock()

    def hold(i):
        seen.append(i["device_id"])
        clock.mono += up.HOLD_BUDGET_S / 2
        return "settled"

    c, _, _ = make(state, relay, hold, clock=clock)
    c.step()
    assert c.load()[0]["resume"] is not None
    relay.pages[None] = ([_it(dev=f"{i:032x}") for i in range(10, 16)], pos(1), False)
    seen.clear()
    c.step()
    assert seen[0] == f"{10:032x}"


def test_a_zero_budget_still_judges_one_item_per_turn(state, monkeypatch):
    monkeypatch.setattr(up, "HOLD_BUDGET_S", 0.0)
    relay = Relay(state, {None: ([_it(dev=f"{i:032x}") for i in range(4)], pos(1), False)})
    seen = []
    c, _, _ = make(state, relay, lambda i: seen.append(i["device_id"]) or "settled")
    for _ in range(6):
        c.step()
    assert relay.acked == [pos(1)] and len(seen) == 4


def test_the_scan_long_threshold_is_1500():
    assert up.SCAN_LONG_PAGES == 1500  # N10: every other test patches it


def test_a_long_scan_continues_from_its_position_and_alarms_once(state, monkeypatch):
    monkeypatch.setattr(up, "SCAN_LONG_PAGES", 3)
    total = 12
    pages = {None: ([_it(dev=f"{1:032x}")], pos(1), True)}
    for k in range(1, total):
        pages[pos(k)] = ([_it(dev=f"{k + 1:032x}")], pos(k + 1), k < total - 1)
    relay = Relay(state, pages)
    seen = []
    c, clock, alarms = make(state, relay, lambda i: seen.append(i["device_id"]) or "settled")
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
    assert c.load()[0]["scan"] is None and c.load()[0]["scan_pages"] == 0  # a completed read resets the counter (N29)


def test_a_scan_is_never_restarted_by_its_length(state):
    pages = {None: ([], pos(1), True)}
    for k in range(1, 200):
        pages[pos(k)] = ([], pos(k + 1), k < 199)
    relay = Relay(state, pages)
    c, _, clock, _ = fake(state, relay)
    for _ in range(230):
        clock.mono += up.BACKOFF_S + 1
        c.step()
    from_start = [c_ for c_ in relay.calls if c_[0] == "GET" and c_[1].endswith("unpublished?limit=64")]
    assert len(from_start) == 1 and c.load()[0]["scan"] is None and c.load()[0]["cursor"] == pos(200)


# -- THE LEDGER: every cell of the 11A.9 table -----------------------------------------------------------------------


def test_cell_a_new_item_with_no_revocation_becomes_a_row_with_one_alarm(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, st, _, alarms = fake(state, relay)
    c.step()
    (row,) = rows(c).values()
    assert (row["need"], row["ver"], row["state"]) == ("compromised", 1, "revoke")
    assert [a[0] for a in alarms] == ["unpublished_held"] and relay.acked == [pos(1)]


def test_cell_an_item_the_state_already_covers_is_settled_and_makes_no_row(state):
    relay = Relay(state, {None: ([_it(reason="lost")], pos(1), False)})
    c, st, _, alarms = fake(state, relay)
    st.state[("d_" + A, "p_" + P1)] = "retired"  # equal strength covers
    c.step()
    assert rows(c) == {} and alarms == []


def test_cell_a_weaker_state_revocation_gives_a_row_in_standing_stronger(state):
    relay = Relay(state, {None: ([_it(reason="compromised")], pos(1), False)})
    c, st, _, _ = fake(state, relay)
    st.state[("d_" + A, "p_" + P1)] = "lost"
    c.step()
    assert rows(c)[(A, P1)]["state"] == "stronger"


@pytest.mark.parametrize("kind", ["other", "unknown"])
def test_cell_other_and_unknown_rows_stay_visible_and_are_not_settled(state, kind):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    st.kind[("d_" + A, "p_" + P1)] = kind
    c.step()
    for _ in range(3):
        clock.mono += up.JUDGE_EVERY_S + 1
        c.step()
    assert rows(c)[(A, P1)]["state"] == kind


def test_cell_a_re_served_equal_item_changes_nothing_and_alarms_nothing(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, _, clock, alarms = fake(state, relay)
    c.step()
    before, n = rows(c), len(alarms)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert rows(c) == before and len(alarms) == n


def test_cell_a_re_served_weaker_item_never_lowers_or_removes(state):
    relay = Relay(state, {None: ([_it(reason="compromised")], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    c.step()
    relay.pages[None] = ([_it(reason="lost")], pos(1), False)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert rows(c)[(A, P1)]["need"] == "compromised" and rows(c)[(A, P1)]["ver"] == 1


def test_cell_a_re_served_stronger_item_raises_the_need_and_the_version(state):
    relay = Relay(state, {None: ([_it(reason="lost")], pos(1), False)})
    c, st, clock, alarms = fake(state, relay)
    c.step()
    relay.pages[None] = ([_it(reason="compromised", digest="sha256:" + "2" * 64)], pos(1), False)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    row = rows(c)[(A, P1)]
    assert (row["need"], row["ver"], row["digest"]) == ("compromised", 2, "sha256:" + "2" * 64)
    assert [a[0] for a in alarms].count("unpublished_held") == 2


def test_cell_sequence_lost_compromised_lost_again(state):
    relay = Relay(state, {None: ([_it(reason="lost")], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    steps = [("lost", "lost", 1), ("compromised", "compromised", 2), ("lost", "compromised", 2)]
    for reason, need, ver in steps:
        relay.pages[None] = ([_it(reason=reason)], pos(1), False)
        clock.ms += up.FULL_EVERY_MS
        c.step()
        assert (rows(c)[(A, P1)]["need"], rows(c)[(A, P1)]["ver"]) == (need, ver)
    st.state[("d_" + A, "p_" + P1)] = "lost"
    clock.mono += up.JUDGE_EVERY_S + 1
    c.step()
    assert rows(c)[(A, P1)]["state"] == "stronger"  # lost does not cover need compromised
    st.commit("d_" + A, "p_" + P1, "compromised")  # the owner signs the stronger one
    clock.mono += up.JUDGE_EVERY_S + 1
    c.step()
    assert rows(c) == {}  # settled, removed by the store's state alone
    relay.pages[None] = ([_it(reason="compromised"), _it(reason="lost")], pos(1), False)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert rows(c) == {}  # both re-served items are settled


def test_cell_an_omitted_item_and_a_complete_scan_without_it_change_no_row(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, _, clock, _ = fake(state, relay)
    c.step()
    relay.pages[None] = ([], pos(1), False)  # the relay forgets it: an EMPTY complete scan
    clock.ms += up.FULL_EVERY_MS
    tick(c, clock, 2)
    assert (A, P1) in rows(c) and c.load()[0]["last_complete_ms"] == clock.ms


def test_cell_an_item_the_relay_perturbs_is_counted_not_dropped_silently(state):
    relay = Relay(state, {None: ([_it(bad=True), _it(dev=B, stranger=True)], pos(1), False)})
    c, _, _, alarms = fake(state, relay)
    c.step()
    u = c.load()[0]["unverifiable"]
    assert u["n"] == 2 and u["clean_scans"] == 0 and relay.acked == [pos(1)]
    assert [a[0] for a in alarms] == ["unpublished_unverifiable"]  # one alarm per page, not per item


def test_cell_unverifiable_is_sticky_an_empty_scan_never_clears_it(state):  # H1
    relay = Relay(state, {None: ([_it(bad=True)], pos(1), False)})
    c, _, clock, _ = fake(state, relay)
    c.step()
    relay.pages[None] = ([], pos(1), False)
    for _ in range(4):  # many complete empty scans, but less than 7 days
        clock.ms += up.FULL_EVERY_MS
        tick(c, clock, 2)
    u = c.load()[0]["unverifiable"]
    assert u["n"] == 1 and u["clean_scans"] >= up.UNVERIFIABLE_CLEAN_SCANS  # clean scans alone do not clear it
    clock.ms += 4 * up.FULL_EVERY_MS  # now 7+ days since last seen
    tick(c, clock, 2)
    assert c.load()[0]["unverifiable"]["n"] == 0


def test_cell_a_new_sighting_resets_the_unverifiable_expiry(state):
    relay = Relay(state, {None: ([_it(bad=True)], pos(1), False)})
    c, _, clock, _ = fake(state, relay)
    c.step()
    clock.ms += 8 * 86400 * 1000
    tick(c, clock, 2)  # the daily read sees it again
    u = c.load()[0]["unverifiable"]
    assert u["n"] == 2 and u["clean_scans"] == 0 and u["last_seen_ms"] == clock.ms


def test_cell_restart_keeps_the_ledger_and_seeds_it_from_the_store(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    ev = [{"device_id": B, "person_id": P1, "reason": "lost", "digest": "sha256:" + "3" * 64}]
    c, _, _, _ = fake(state, relay, evidence=lambda: ev)
    c.step()
    assert {d for d, _p in rows(c)} == {A, B}  # the store's durable evidence joined
    c2, _, _, _ = fake(state, relay, evidence=lambda: ev)  # a restart
    c2.step()
    assert {d for d, _p in rows(c2)} == {A, B}


def test_cell_a_lost_ledger_file_means_rebuilding_until_a_complete_scan(state):  # H2
    relay = Relay(state, {None: ([_it()], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, clock, alarms = fake(state, relay)
    tick(c, clock, 2)
    assert not c.load()[0]["rebuilding"] and (A, P1) in rows(c)
    state.remove(up.FILE)  # the ledger file is lost; its mirror remains
    c2, _, clock2, alarms2 = fake(state, relay, evidence=lambda: [])
    assert c2.load()[1] is True
    c2.step()
    assert c2.load()[0]["rebuilding"] and ("unpublished_rebuilding",) == alarms2[0][:1]
    for _ in range(3):
        clock2.mono += up.BACKOFF_S + 1
        c2.step()
    assert not c2.load()[0]["rebuilding"] and (A, P1) in rows(c2)  # rebuilt from the relay's view
    assert up.ledger_view(json.loads(state.read(up.FILE)), clock2.ms)["rebuilding"] is False


def test_cell_status_is_not_ok_while_rebuilding(state):
    relay = Relay(state, {None: ([], pos(1), True), pos(1): ([], pos(2), False)})
    c, _, clock, _ = fake(state, relay)
    tick(c, clock, 2)
    state.remove(up.FILE)
    c2, _, clock2, _ = fake(state, relay)
    c2.step()
    view = up.ledger_view(json.loads(state.read(up.FILE)), clock2.ms)
    assert view["rebuilding"] is True and relayr4.held_attention("X", [], view) is not None


def test_cell_an_older_file_with_a_newer_mirror_means_rebuilding_a_missing_mirror_too(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, _, clock, _ = fake(state, relay)
    c.step()
    old = state.read(up.FILE)
    clock.ms += up.FULL_EVERY_MS
    c.step()
    assert c.load()[1] is False  # control: file and mirror agree
    state.write(up.FILE, old)  # an OLDER file next to the newer mirror
    assert c.load()[1] is True
    # the file ahead of its mirror is only a crash window: no loss
    seq = json.loads(state.read(up.FILE))["ledger_seq"]
    state.write_json(up.MIRROR, {"seq": seq - 1})
    assert c.load()[1] is False
    state.remove(up.MIRROR)  # a missing mirror next to a present file: rebuilding
    assert c.load()[1] is True


def test_cell_a_damaged_file_or_row_is_a_loss_and_keeps_the_valid_rows(state):
    relay = Relay(state, {None: ([_it(), _it(dev=B)], pos(2), False)})
    c, _, _, _ = fake(state, relay)
    c.step()
    doc = json.loads(state.read(up.FILE))
    doc["held"][1]["need"] = "bogus"
    state.write_json(up.FILE, doc)
    st, lost = c.load()
    assert lost is True and [r["device_id"] for r in st["held"]] == [A]
    state.write(up.FILE, b"{not json")
    assert c.load()[1] is True


def test_cell_a_fresh_install_is_not_a_loss(state):
    c, _, _, _ = fake(state, Relay(state))
    assert c.load()[1] is False


def test_cell_a_raise_racing_a_settle_is_refused_by_the_compare_and_set(state):  # H4
    relay = Relay(state, {None: ([_it(reason="lost")], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    c.step()
    stale = [{"device_id": A, "person_id": P1, "need": "lost", "ver": 1, "standing": "settled"}]
    cur = c.load()[0]
    led = up.Ledger(cur, relayr4.stronger)
    led.add_or_raise({**up.row_of(_it(reason="compromised"), 5), "ver": 1})  # raised to compromised: ver 2
    assert led.settle_from_store(stale) is False  # the answer judged need lost / ver 1
    assert [r["need"] for r in cur["held"]] == ["compromised"]
    assert led.settle_from_store([{**stale[0], "need": "compromised", "ver": 2}]) is True and cur["held"] == []


def test_cell_a_store_commit_settles_at_the_next_turn_and_a_wake_forces_the_judging(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    c.step()
    st.commit("d_" + A, "p_" + P1, "compromised")
    c.step()  # judged less than JUDGE_EVERY_S ago: not yet
    assert (A, P1) in rows(c)
    c.wake()
    c.step()
    assert rows(c) == {}


def test_cell_the_ledger_is_judged_even_while_the_relay_backs_off(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, st, clock, _ = fake(state, relay)
    c.step()
    relay.fail[GET] = RelayRefused(500, "x")
    clock.ms += up.FULL_EVERY_MS
    c.step()  # the daily read fails: back-off
    st.commit("d_" + A, "p_" + P1, "compromised")
    clock.mono += up.JUDGE_EVERY_S + 1
    c.step()
    assert rows(c) == {}


def test_cell_without_host_state_every_row_is_unknown_and_nothing_is_removed(state):
    relay = Relay(state, {None: ([_it()], pos(1), False)})
    c, clock, _ = make(state, relay, lambda i: "held")  # no judge at all
    c.step()
    clock.mono += up.JUDGE_EVERY_S + 1
    c.step()
    assert rows(c)[(A, P1)]["state"] == "unknown"


def test_cell_the_ledger_overflows_without_evicting(state, monkeypatch):
    monkeypatch.setattr(up, "LEDGER_MAX", 3)
    items = [_it(dev=f"{i:032x}") for i in range(5)]
    relay = Relay(state, {None: (items, pos(1), False)})
    c, _, _, alarms = fake(state, relay)
    c.step()
    st = c.load()[0]
    assert [r["device_id"] for r in st["held"]] == [f"{i:032x}" for i in range(3)] and st["overflow"] == 2
    assert sum(a[0] == "unpublished_overflow" for a in alarms) == 1
    view = up.ledger_view(json.loads(state.read(up.FILE)), 0)
    assert view["overflow"] == 2 and relayr4.held_attention("X", view["held"], view)["fix"]
    relay.pages[None] = ([_it(dev=f"{i:032x}") for i in range(5)], pos(1), False)
    c.step()
    assert c.load()[0]["overflow"] >= 2  # never cleared by relay behaviour


def test_cell_status_goes_stale_after_failed_turns_or_an_old_scan(state):
    relay = Relay(state)
    c, _, clock, _ = fake(state, relay)
    c.step()
    for _ in range(up.FAILED_TURNS_STALE):
        relay.fail[GET] = RelayRefused(500, "x")
        clock.ms += up.FULL_EVERY_MS
        clock.mono += up.BACKOFF_S + 1
        c.step()
    view = up.ledger_view(json.loads(state.read(up.FILE)), clock.ms)
    assert view["stale"] is True and relayr4.held_attention("X", [], view)["fix"] == relayr4.FIX_STALE
    doc = {"last_complete_ms": 1000}
    assert up.ledger_view(doc, 1000 + up.STALE_MS + 1)["stale"] is True
    assert up.ledger_view(doc, 1000 + up.STALE_MS)["stale"] is False
    assert up.ledger_view({"last_complete_ms": 0}, 10**12)["stale"] is False  # never completed: rebuilding says so


HOSTILE = ["\n", "\x1b[2J", 5, None, "x" * 500, "é", "a&b", "", ["x"], {"k": 1}, b"\x00"]


@pytest.mark.parametrize("bad", HOSTILE)
@pytest.mark.parametrize("field", ["device_id", "person_id", "reason", "digest", "chain_reason"])
def test_hostile_fields_never_hide_a_held_notice(state, field, bad):  # N26
    it = _it()
    if field == "digest":
        it["provenance"] = {"cursor": 1, "digest": bad}
    else:
        it[field] = bad
    relay = Relay(state, {None: ([it], pos(1), False)})
    c, _, alarms = make(state, relay, lambda i: "held")
    c.step()
    (row,) = up.held_pairs(json.loads(state.read(up.FILE)))  # a row exists and passes the status reader's checks
    assert alarms[-1][0] == "unpublished_held" and relay.acked == [pos(1)] and alarms[-1][1].isprintable()
    assert row["need"] in ("compromised", "lost", "retired", "unknown")
    if field == "reason":
        assert row["need"] == "unknown"


def test_an_unknown_reason_is_never_lowered_and_counts_as_the_strongest():
    assert relayr4.stronger("unknown", "compromised") and not relayr4.stronger("compromised", "unknown")
    assert not relayr4.stronger("unknown", "unknown")


@pytest.mark.parametrize("new", certs.REVOKE_REASONS)
@pytest.mark.parametrize("old", certs.REVOKE_REASONS)
def test_the_injected_comparison_agrees_with_revoke_strength(new, old):  # 3x3
    assert relayr4.stronger(new, old) == (certs.revoke_strength(new) > certs.revoke_strength(old))


def test_the_fix_text_follows_the_rows_standing_never_the_relay():
    base = {
        "device_id": A,
        "person_id": P1,
        "need": "compromised",
        "ver": 1,
        "digest": "sha256:" + "1" * 64,
        "at_ms": 1,
    }
    texts = {
        s: relayr4.held_attention("X", [{**base, "state": s}])["fix"]
        for s in ("revoke", "stronger", "other", "unknown")
    }
    assert texts["revoke"] == relayr4.FIX_REVOKE and texts["stronger"] == relayr4.FIX_STRONGER
    assert texts["other"] == relayr4.FIX_OTHER and texts["unknown"] == relayr4.FIX_UNKNOWN
    assert "cannot be settled from this host" in texts["other"] and "nothing to sign" in texts["unknown"]
    for t in texts.values():
        assert "re-append" not in t and "again" not in t
    both = relayr4.held_attention("X", [{**base, "state": "revoke"}, {**base, "state": "stronger", "device_id": B}])
    assert relayr4.FIX_REVOKE in both["fix"] and relayr4.FIX_STRONGER in both["fix"]
    assert relayr4.held_attention("X", []) is None


# -- the real store: end to end and adversarial ----------------------------------------------------------------------


@pytest.fixture
def ws(tmp_path):
    env = Env(tmp_path / "w")
    env.bootstrap()
    device, sig_pub = env.add_device()
    try:
        yield env, device, sig_pub
    finally:
        env.store.close()


def _seam(env, state, relay):
    """The real ``relayr4.unpublished`` seam over a real store (the job runs inline)."""
    from types import SimpleNamespace

    alarms: list[tuple[str, str]] = []
    svc = SimpleNamespace(workspace_id=WS, store=env.store, clock=lambda: T0)
    worker = SimpleNamespace(session=relay, state=state, job=lambda fn: fn(), loop=None,
                             alarm=lambda code, detail: alarms.append((code, detail)))  # fmt: skip
    return relayr4.unpublished(svc, worker), alarms


def _real(env, state, items, *, more=False):
    relay = Relay(state, {None: (items, pos(1), more)})
    c, alarms = _seam(env, state, relay)
    return c, relay, alarms


def test_real_store_forgeries_are_unverifiable_and_counted_the_genuine_pair_is_a_row(ws, state):
    env, device, sig_pub = ws
    good = item(env.owner, sig_pub, reason="lost")
    sub = item(env.owner, sig_pub, reason="lost")
    sub["reason"] = "compromised"
    forged = item(env.owner, sig_pub, reason="lost")
    forged["revocation"]["o"]["reason"] = "compromised"
    other = item(env.owner, sig_pub, cert=_cert(Person(), sig_pub))
    old = item(env.owner, sig_pub, cert=_cert(env.owner, sig_pub, created_ms=(T0 - 10 * 86400) * 1000,
                                               expires_ms=(T0 - 9 * 86400) * 1000))  # fmt: skip
    stray = item(env.owner, crypto.public_bytes(crypto.generate_private_key()))
    c, relay, alarms = _real(env, state, [sub, forged, other, good, stray, old])
    c.step()
    st = c.load()[0]
    assert st["unverifiable"]["n"] == 3  # the substituted outer field, the forged revocation, another person's cert
    held = {(r["device_id"], r["need"], r["state"]) for r in st["held"]}
    assert (device[2:], "compromised", "stronger" if False else "revoke") in held or (
        device[2:],
        "lost",
        "revoke",
    ) in held
    assert any(r["state"] == "unknown" for r in st["held"])  # the unlisted device: a visible `unknown` row (no pending)
    assert relay.acked == [pos(1)] and any(a[0] == "unpublished_held" for a in alarms)


def test_real_store_a_wrong_outer_field_on_a_genuine_revocation_is_counted_never_silent(ws, state):  # E5
    env, _device, sig_pub = ws
    it = item(env.owner, sig_pub, reason="lost")
    it["reason"] = "retired"
    c, relay, alarms = _real(env, state, [it])
    c.step()
    assert c.load()[0]["unverifiable"]["n"] == 1 and any(a[0] == "unpublished_unverifiable" for a in alarms)


def test_real_store_a_cap_rejection_stays_in_the_owners_view(ws, state, monkeypatch):
    from orch.store import pins as pins_mod

    env, _device, _sig_pub = ws
    monkeypatch.setattr(pins_mod.HostPins, "note_revocation", lambda *a, **k: False)
    c, relay, _ = _real(env, state, [item(env.owner, crypto.public_bytes(crypto.generate_private_key()))])
    c.step()
    assert len(c.load()[0]["held"]) == 1


def test_real_store_a_crash_before_the_ack_reads_again_and_judges_idempotently(ws, state):
    env, device, sig_pub = ws
    c, relay, _ = _real(env, state, [item(env.owner, sig_pub)])
    relay.fail[ACK] = TransportError("deadline", "test")
    c.step()
    before = rows(c)
    c._not_before = 0.0  # noqa: SLF001
    c._mono = lambda: 10**9  # noqa: SLF001
    c._state.write_json(up.FILE, {**c.load()[0], "last_full_ms": 0})  # noqa: SLF001
    c.step()
    assert rows(c) == before and relay.acked == [pos(1)]


def test_real_store_the_owner_signing_settles_the_row(ws, state):
    env, device, sig_pub = ws
    c, relay, _ = _real(env, state, [item(env.owner, sig_pub)])
    c.step()
    assert len(c.load()[0]["held"]) == 1
    env.revoke_device(device, reason="compromised")
    c.wake()
    c._mono = lambda: 10**9  # noqa: SLF001
    c.step()
    assert c.load()[0]["held"] == []


def test_real_store_through_the_seam_a_weaker_chain_pair_stays_visible_and_is_settled_by_the_upgrade(ws, state):
    env, device, sig_pub = ws
    env.revoke_device(device, reason="lost")
    c, relay, alarms = _real(env, state, [item(env.owner, sig_pub, reason="compromised")])
    c.step()
    (row,) = c.load()[0]["held"]
    assert (row["need"], row["state"]) == ("compromised", "stronger")
    note = relayr4.held_attention("X", [row])
    assert note["fix"] == relayr4.FIX_STRONGER
    env.revoke_device(device, reason="compromised")
    c.wake()
    c._mono = lambda: 10**9  # noqa: SLF001
    c.step()
    assert c.load()[0]["held"] == []


def _tombstone(env, person, key, reason):
    """A restore that carries ``person``'s revocation of ``key``: the device exists only as a tombstone."""
    from tests.store.test_revocation_pairs import _evidence

    rev, cert = _evidence(person, key, reason)
    facts = env.store.restore_facts("workspace")
    facts["revocations"] = [{"revocation": rev, "device_cert": cert, "pk_pub": crypto.b64u(person.pk_pub)}]
    env.store.append(env.person_event(env.owner, "workspace", "restore", **facts, reason="other"), log="workspace")
    return certs.device_ref(key), "p_" + crypto.person_id(person.pk_pub).hex()


@pytest.mark.parametrize("held", ["lost", "retired"])
def test_real_store_E1_a_compromised_upgrade_over_a_tombstone_is_a_visible_row(ws, state, held):
    env, _device, _sig = ws
    gone, key = Person(), crypto.public_bytes(crypto.generate_private_key())
    dev, person = _tombstone(env, gone, key, held)
    assert env.store.pair_standings([(dev, person, "compromised")]) == ["stronger"]
    c, relay, alarms = _real(env, state, [item(gone, key, reason="compromised")])
    c.step()
    (row,) = c.load()[0]["held"]
    assert (row["need"], row["state"]) == ("compromised", "stronger") and alarms[-1][0] == "unpublished_held"
    assert env.store.pair_standings([(dev, person, "lost")]) == ["settled"]  # equal strength covers a lost item


def test_real_store_a_tombstone_with_only_other_persons_evidence_is_other(ws, state):  # DEVICE_CERT
    env, _device, _sig = ws
    gone, key = Person(), crypto.public_bytes(crypto.generate_private_key())
    dev, _person = _tombstone(env, gone, key, "lost")
    c, relay, _ = _real(env, state, [item(env.owner, key, reason="compromised")])  # the OWNER also certifies that key
    c.step()
    (row,) = c.load()[0]["held"]
    assert row["state"] == "other" and relayr4.held_attention("X", [row])["fix"] == relayr4.FIX_OTHER


def test_real_store_an_unknown_device_is_a_row_in_standing_unknown_and_a_former_member_is_a_normal_row(ws, state):
    env, _device, _sig = ws
    c, relay, _ = _real(env, state, [item(env.owner, crypto.public_bytes(crypto.generate_private_key()))])
    c.step()
    assert [r["state"] for r in c.load()[0]["held"]] == ["unknown"]
    mara = _add_member_later(env)
    key = crypto.public_bytes(crypto.generate_private_key())
    env.store.append(env.person_event(env.owner, "workspace", "member.removed", person=mara.ref), log="workspace")
    assert env.store.note_forwarded_revocation(item(mara, key)) == "held"  # a FORMER member: still verifiable


def _add_member_later(env):
    mara = Person()
    env.store.append(
        env.person_event(
            env.owner, "workspace", "member.added", person=mara.ref, name="Mara", role="member",
            pk_pub=crypto.b64u(mara.pk_pub), device_cert=mara.cert(created=T0 * 1000),
        ),
        log="workspace",
    )  # fmt: skip
    return mara


def test_real_store_E2_a_re_served_chain_embedded_weaker_item_never_erases_the_stronger_row(ws, state):
    env, device, sig_pub = ws
    env.revoke_device(device, reason="lost")
    strong, weak = item(env.owner, sig_pub, reason="compromised"), item(env.owner, sig_pub, reason="lost")
    c, relay, _ = _real(env, state, [strong, weak])  # the weak one is the chain's own, public revocation
    c.step()
    (row,) = c.load()[0]["held"]
    assert (row["need"], row["state"]) == ("compromised", "stronger")


def test_real_store_E3_a_perturbed_or_omitted_re_serve_never_removes_a_row(ws, state):
    env, device, sig_pub = ws
    good = item(env.owner, sig_pub)
    c, relay, _ = _real(env, state, [good])
    c.step()
    assert len(c.load()[0]["held"]) == 1
    bad = dict(good, reason="lost")  # one wrong outer field: the store cannot verify it
    for page in ([bad], []):
        relay.pages[None] = (page, pos(1), False)
        st = c.load()[0]
        st["last_full_ms"] = 0
        c._save(st)  # noqa: SLF001
        c._mono = lambda: 10**9  # noqa: SLF001
        c.step()
        assert len(c.load()[0]["held"]) == 1  # the row stays: only the store's own state can settle it


def test_real_store_seeding_includes_an_upgrade_kept_beside_a_chain_record(ws):
    env, device, sig_pub = ws
    env.revoke_device(device, reason="lost")
    assert env.store.note_forwarded_revocation(item(env.owner, sig_pub, reason="compromised")) == "held"
    ev = env.store.forwarded_evidence()
    assert {(e["device_id"], e["reason"]) for e in ev} == {(device[2:], "compromised")}  # the `upgrade` entry


# -- structure -------------------------------------------------------------------------------------------------------


def _tree():
    import ast
    import inspect

    src = inspect.getsource(up)
    tree = ast.parse(src)
    return tree, {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


LEDGER_KEYS = {"held", "unverifiable", "overflow", "rebuilding", "last_complete_ms", "failed_turns", "ledger_seq"}


def test_structure_only_the_ledger_class_writes_the_ledger_fields():
    import ast

    tree, _ = _tree()
    owners: dict[str, set[str]] = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in ast.walk(cls) if isinstance(n, ast.FunctionDef)]:
            for n in ast.walk(fn):
                key = None
                if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.slice, ast.Constant):
                    key = n.slice.value
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("append", "remove", "update", "pop", "clear", "extend", "insert")
                ):
                    inner = n.func.value
                    if isinstance(inner, ast.Subscript) and isinstance(inner.slice, ast.Constant):
                        key = inner.slice.value
                    if n.func.attr == "update":  # cur.update(...) on a row inside the ledger class
                        key = key or ("row" if cls.name == "Ledger" else None)
                if key in LEDGER_KEYS or (key == "row"):
                    owners.setdefault(cls.name, set()).add(fn.name)
    # the consumer writes a ledger field only while LOADING the persisted record and bumping the sequence on save
    assert owners.pop("UnpublishedConsumer", set()) <= {"load", "_save"}, owners
    assert set(owners) == {"Ledger"}, owners
    # module-level functions never write them either, apart from the fresh record builder
    _, fns = _tree()
    for name, fn in fns.items():
        if name in {
            n.name
            for n in ast.walk([c for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "Ledger"][0])
            if isinstance(n, ast.FunctionDef)
        }:
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.slice, ast.Constant):
                assert n.slice.value not in LEDGER_KEYS - {"ledger_seq"} or name in ("load", "_empty"), (
                    name,
                    n.slice.value,
                )


def test_structure_ledger_seq_is_written_only_by_save_and_load():
    import ast

    _, fns = _tree()
    writers = {
        name for name, fn in fns.items() for n in ast.walk(fn)
        if (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store) and isinstance(n.slice, ast.Constant)
            and n.slice.value == "ledger_seq")
        or (isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Subscript)
            and isinstance(n.target.slice, ast.Constant) and n.target.slice.value == "ledger_seq")
    }  # fmt: skip
    assert writers == {"_save"}


def test_structure_the_ledger_has_one_removal_and_the_consumer_has_none():
    import ast

    tree, fns = _tree()
    removers = {
        name for name, fn in fns.items()
        if any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "remove"
            for n in ast.walk(fn)
        )
    }  # fmt: skip
    assert removers == {"settle_from_store"}
    assert "'settled'" not in ast.unparse(fns["step"]) and "'settled'" in ast.unparse(fns["_ingest"])


def test_structure_the_consumer_reaches_no_store_or_key_directly():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(up))
    bad = ("orch.store", "orch.custody", "orch.identity", "orch.crypto")
    imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)] + [
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    ]
    assert not [m for m in imported if m.startswith(bad)]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not {"note_forwarded_revocation", "pair_standings", "pair_settled", "forwarded_evidence"} & names


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
            have = env.store._state.revoked_reason(device, "p_" + crypto.person_id(env.owner.pk_pub).hex())  # noqa: SLF001
            assert have == (new if certs.revoke_strength(new) > certs.revoke_strength(prev) else prev), (prev, new)
    with pytest.raises(KeyError):
        certs.revoke_strength("unknown")


# -- the model-based test --------------------------------------------------------------------------------------------


class ViewRelay(Relay):
    """A relay serving a derived VIEW (a list of items) in pages of three, as the real relay's view is served."""

    def __init__(self, state):
        super().__init__(state)
        self.view: list[dict] = []
        self.index = {None: 0}

    def call(self, method, path, route, **kw):
        if method == "POST":
            self.acked.append(kw["body"]["cursor"])
            return {"acked": kw["body"]["cursor"]}
        params = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
        start = self.index.get(params.get("cursor"))
        if start is None:
            raise RelayRefused(409, "cursor_ahead")
        chunk = self.view[start : start + 3]
        end = start + len(chunk)
        self.index[pos(end + 1000)] = end
        return {"items": chunk, "cursor": pos(end + 1000), "more": end < len(self.view)}


PAIRS = [(f"{i:032x}", P1 if i % 2 else P2) for i in range(6)]


@pytest.mark.parametrize("seed", range(40))
def test_model_based_the_ledger_never_loses_an_obligation_the_store_has_not_settled(tmp_path, seed):
    rng = random.Random(seed)
    sdir = StateDir(tmp_path / "relay")
    relay, fs, clock = ViewRelay(sdir), FakeStore(), Clock()
    capped: set[tuple[str, str]] = set()  # pairs HostPins rejected at its cap: not in the seed

    def evidence():
        return [
            {"device_id": d[2:], "person_id": p[2:], "reason": r, "digest": "sha256:" + "4" * 64}
            for (d, p), r in fs.delivered.items() if (d, p) not in capped
        ]  # fmt: skip

    alarms: list = []

    def new_consumer():
        return up.UnpublishedConsumer(
            workspace_id=WS,
            session=relay,
            state=sdir,
            hold=fs.hold,
            judge=fs.judge,
            evidence=evidence,
            stronger=relayr4.stronger,
            notices=lambda: None,
            alarm=lambda c_, d_: alarms.append(c_),
            now_ms=clock.now,
            mono=lambda: clock.mono,
        )

    c = new_consumer()
    snapshots: list[bytes] = []

    def ledger():
        doc = json.loads(sdir.read(up.FILE) or b"{}")
        return {(r["device_id"], r["person_id"]): r for r in doc.get("held", [])}, doc.get("rebuilding", False)

    def need_of(d_, p_):
        return fs.delivered[(d_, p_)]

    def check(strict: bool):
        led, rebuilding = ledger()
        for (d_, p_), reason in fs.delivered.items():
            if (
                certs.revoke_strength(fs.state.get((d_, p_), "x")) >= certs.revoke_strength(reason)
                if (d_, p_) in fs.state
                else False
            ):
                continue  # settled by the store's own state
            row = led.get((d_[2:], p_[2:]))
            if row is None:
                assert rebuilding and not strict, (seed, d_, p_, "obligation lost while not rebuilding")
            else:
                assert not relayr4.stronger(reason, row["need"]), (seed, "row weaker than the delivered obligation")
        for row in led.values():
            assert row["need"] in certs.REVOKE_REASONS  # only verified reasons ever got in

    prev_rows: dict = {}
    for _ in range(30):
        op = rng.choice(
            ["serve", "serve", "tick", "tick", "tick", "commit", "restart", "lose", "crash", "omit", "empty"]
        )
        d, p = PAIRS[rng.randrange(len(PAIRS))]
        d, p = "d_" + d, "p_" + p
        reason = rng.choice(certs.REVOKE_REASONS)
        if op == "serve":
            relay.view.append(_it(dev=d[2:], person=p[2:], reason=reason))
            if rng.random() < 0.2:
                capped.add((d, p))
        elif op == "commit":
            fs.commit(d, p, reason)
        elif op == "restart":
            c = new_consumer()
        elif op == "lose":
            kind = rng.choice(["file", "older", "mirror"])
            if kind == "file" and sdir.read(up.FILE) is not None:
                sdir.remove(up.FILE)
            elif kind == "older" and snapshots:
                sdir.write(up.FILE, snapshots[0])
            elif kind == "mirror" and sdir.read(up.MIRROR) is not None:
                sdir.remove(up.MIRROR)
            c = new_consumer()
            clock.mono += up.BACKOFF_S + 1
            try:
                c.step()  # the consumer meets the loss: `rebuilding` is set, never a silently smaller view
            except RuntimeError:
                pass
            prev_rows = ledger()[0]
        elif op == "crash":
            fs.raise_at = fs.calls + rng.randint(1, 3)
        elif op == "omit":  # withholding BEFORE first delivery is outside the invariant: drop never-delivered items
            relay.view = [
                i
                for i in relay.view
                if ("d_" + i["device_id"], "p_" + i["person_id"]) in fs.delivered or rng.random() < 0.5
            ]
        elif op == "empty":
            relay.view = [i for i in relay.view if ("d_" + i["device_id"], "p_" + i["person_id"]) in fs.delivered]
        if op == "tick" or rng.random() < 0.5:
            clock.mono += up.BACKOFF_S + 1
            clock.ms += rng.choice([0, 1000, up.FULL_EVERY_MS])
            try:
                c.step()
            except RuntimeError:
                pass
            if sdir.read(up.FILE) is not None and (not snapshots or rng.random() < 0.3):
                snapshots.append(sdir.read(up.FILE))
        led_now, _ = ledger()
        for key in set(prev_rows) - set(led_now):  # every disappearance is justified by the store's own state
            d_, p_ = "d_" + key[0], "p_" + key[1]
            assert (
                (d_, p_) in fs.state
                and certs.revoke_strength(fs.state[(d_, p_)]) >= certs.revoke_strength(prev_rows[key]["need"])
                or rebuilding_started(sdir)
            ), (seed, key)
        prev_rows = led_now
        check(strict=False)
    # settle phase: no faults, a complete view, many turns: the strict invariant must hold and nothing is rebuilding
    fs.raise_at = None
    relay.view = [i for i in relay.view if ("d_" + i["device_id"], "p_" + i["person_id"]) in fs.delivered] + [
        _it(dev=d[2:], person=p[2:], reason=r) for (d, p), r in fs.delivered.items()
    ]
    c = new_consumer()
    for _ in range(40):
        clock.mono += up.BACKOFF_S + 1
        clock.ms += 1000
        c.step()
    check(strict=True)
    assert ledger()[1] is False


def rebuilding_started(sdir):
    doc = json.loads(sdir.read(up.FILE) or b"{}")
    return doc.get("rebuilding", False)
