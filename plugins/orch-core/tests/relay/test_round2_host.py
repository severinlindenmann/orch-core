"""R2 round 2 on the real host (``RelayEntry`` on a served workspace): ruling A against the host clock, store errors as
refusals (R2-6), the review's crash points (exactly once; kills M14/M15), and a fault-injected append (the log check
before the outcome is classified; kills M16)."""

from __future__ import annotations

import pytest

from orch.relay.framing import Refusal
from tests.host.human_helpers import lab, served  # noqa: F401 - fixtures

from .test_relay_entry import _code, _entry, _ident, _new_ticket, _on_worker, _submit


def test_a_committed_event_resent_after_the_window_replays_against_host_time(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    first = _submit(served, entry, owner, log, ev)
    served.lab.ws.clock[0] += 901  # host time: the event id is now outside the window
    again = _submit(served, entry, owner, log, ev)
    assert again["duplicate"] is True and again["data"] == first["data"]


def test_a_logged_event_without_an_outcome_record_is_deduped_before_the_window(served):  # noqa: F811
    """Ruling A's second step: the log dedup (another principal's record or none) comes before the window."""
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    _submit(served, entry, owner, log, ev)
    served.lab.ws.clock[0] += 901
    # the same event under another idem (a fresh outcome key): no record, the log knows the identity
    other_idem = ev["id"][:-1] + ("0" if ev["id"][-1] != "0" else "1")
    ident = _ident(served, owner)
    got = _on_worker(served, lambda: entry.submit(ident, other_idem, "new_ticket", log, [ev], lambda: True))
    assert got["duplicate"] is True


def test_a_never_seen_event_outside_the_host_window_is_event_expired(served):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    served.lab.ws.clock[0] += 901
    assert _code(lambda: _submit(served, entry, owner, log, ev)) == "event.expired"
    assert served.service.store.logged_event(log, ev["id"]) is None
    served.lab.ws.clock[0] -= 901 + 301  # 300 s in the past of the id: also outside (future side)
    log2, ev2 = _new_ticket(served, owner)
    served.lab.ws.clock[0] -= 301
    assert _code(lambda: _submit(served, entry, owner, log2, ev2)) == "event.expired"


def test_store_busy_is_a_class_a_refusal_and_leaves_no_outcome(served, monkeypatch):  # noqa: F811
    from orch.store.lock import LockTimeout

    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    store = served.service.store
    real = store.locked

    def busy():
        raise LockTimeout("held for longer than the timeout")

    monkeypatch.setattr(store, "locked", busy)
    ident = _ident(served, owner)
    with pytest.raises(Refusal) as e:
        _on_worker(served, lambda: entry.submit(ident, ev["id"], "new_ticket", log, [ev], lambda: True))
    assert e.value.code == "store.busy"
    monkeypatch.setattr(store, "locked", real)
    monkeypatch.setattr(entry._backend, "serve", lambda call: busy())  # noqa: SLF001 - a read hitting the lock
    with pytest.raises(Refusal) as e:
        _on_worker(served, lambda: entry.read(ident, "whoami", {}, lambda: True))
    assert e.value.code == "store.busy"
    assert _code(lambda: _on_worker(served, lambda: entry.lookup(ident, ev["id"]))) == "outcome.unknown_idem"
    assert _submit(served, entry, owner, log, ev)["duplicate"] is False  # nothing was admitted on the host


class _Kill(BaseException):
    pass


@pytest.mark.parametrize("point", ["before_append", "after_append", "after_plan", "after_note"])
def test_crash_points_in_the_real_relay_submit_reconcile_exactly_once(served, monkeypatch, point):  # noqa: F811
    import orch.host.relayentry as RE

    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    svc = served.service
    real_append, real_plan, real_note = RE.append_relay, svc.outcomes.plan, svc.outcomes.note

    def append(*a, **k):
        if point == "before_append":
            raise _Kill()
        out = real_append(*a, **k)
        if point == "after_append":
            raise _Kill()
        return out

    def plan(*a, **k):
        real_plan(*a, **k)
        if point == "after_plan":
            raise _Kill()

    def note(*a, **k):
        real_note(*a, **k)
        if point == "after_note":
            raise _Kill()

    monkeypatch.setattr(RE, "append_relay", append)
    monkeypatch.setattr(svc.outcomes, "plan", plan)
    monkeypatch.setattr(svc.outcomes, "note", note)
    ident = _ident(served, owner)
    with pytest.raises(_Kill):
        _on_worker(served, lambda: entry.submit(ident, ev["id"], "new_ticket", log, [ev], lambda: True))
    monkeypatch.setattr(RE, "append_relay", real_append)
    monkeypatch.setattr(svc.outcomes, "plan", real_plan)
    monkeypatch.setattr(svc.outcomes, "note", real_note)
    try:
        st = _on_worker(served, lambda: entry.lookup(ident, ev["id"]))
    except Refusal as r:
        st = {"state": r.code}
    res = _on_worker(served, lambda: entry.submit(ident, ev["id"], "new_ticket", log, [ev], lambda: True))
    assert _on_worker(served, lambda: len(svc.store.events(log))) == 1
    if point == "after_append":  # planned before the append: the restart reconciles it committed
        assert st["state"] == "committed" and res["duplicate"] is True
    else:
        assert st["state"] == "outcome.unknown_idem" and res["duplicate"] is False


def test_an_append_that_did_not_land_as_submitted_is_never_classified_committed(served, monkeypatch):  # noqa: F811
    import orch.host.relayentry as RE

    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    monkeypatch.setattr(RE, "append_relay", lambda *a, **k: ([1], None))  # claims a landing that did not happen
    assert _code(lambda: _submit(served, entry, owner, log, ev)) == "internal"
