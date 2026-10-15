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

    def call(self, method, path, route, **kw):
        snap = self.state.read_json(up.FILE)
        self.calls.append((method, path, kw.get("body"), snap))
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


# -- failures -----------------------------------------------------------------------------------------------------


def test_cursor_ahead_restarts_from_the_start(state):
    relay = Relay(state)
    note = {"unpublished": pos(9)}
    c, _, alarms = make(state, relay, lambda i: "pending", notices=lambda: note)
    c.step()  # full read
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = RelayRefused(409, "cursor_ahead")
    note["unpublished"] = pos(10)
    c.step()
    assert c.load()["scan"] == "" and ("unpublished_cursor_ahead",) == alarms[-1][:1]
    n = len(relay.calls)
    c.step()
    assert relay.calls[n][1].endswith("unpublished?limit=64")


def test_rate_limited_waits_for_retry_after(state):
    relay = Relay(state)
    relay.fail["GET /v2/workspaces/{ws}/unpublished"] = RelayRefused(429, "rate_limited", retry_after=30)
    c, clock, alarms = make(state, relay, lambda i: "pending")
    c.step()
    n = len(relay.calls)
    c.step()
    clock.mono += 29
    c.step()
    assert len(relay.calls) == n
    clock.mono += 2
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
