"""The H12e adapter seam (``orch.host.relayadmit``): core's helpers (#474, merged) under one module, plus one stand-in,
``device_standing`` (H12e-2, #482). Every behaviour test runs against the seam AND against ``orch.host.admission``
(``judge_events`` from ``orch.host.human``); once #482 merges, its ``device_standing`` is checked against the same
contract (review I3a: the real helpers are collected and run, not skipped)."""

from __future__ import annotations

import inspect

import pytest

from orch.host import relayadmit
from orch.host.errors import HostError
from orch.relay import submit
from tests.host.human_helpers import lab, served  # noqa: F401 - fixtures

from .bridgekit import ulid
from .test_relay_entry import _entry, _event, _new_ticket, _next_key, _on_worker, _submit

NAMES = {
    "COLUMN_R_AUTH", "check_target", "check_window", "device_standing", "judge_events", "known_or_fresh", "next_key",
    "signed_digest",
}  # fmt: skip
SIGS = {
    "check_window": ["event_id", "now"],
    "signed_digest": ["workspace_id", "log", "event"],
    "known_or_fresh": ["store", "log", "event", "now"],
    "next_key": ["store"],
    "check_target": ["store", "log", "event"],
    "judge_events": ["store", "log", "events", "git", "copies"],
    "device_standing": ["store", "person_id", "device_id"],
}


def _impls():
    import types

    from orch.host import admission, human

    real = {n: getattr(admission, n, None) for n in NAMES - {"judge_events", "device_standing"}}
    # TODO(#482): admission.device_standing once merged; until then the seam's stand-in is the only one
    real["device_standing"] = getattr(admission, "device_standing", relayadmit.device_standing)
    return [relayadmit, types.SimpleNamespace(**real, judge_events=human.judge_events)]


def test_the_seam_has_h12es_names_and_signatures():
    assert set(relayadmit.__all__) == NAMES
    for name, params in SIGS.items():
        assert list(inspect.signature(getattr(relayadmit, name)).parameters) == params, name
    assert relayadmit.COLUMN_R_AUTH == submit.AUTH  # the relay side's own copy (orch.host is banned there)


def test_the_seam_is_the_real_helpers():
    """D2-5: the round-2 stand-ins are gone: every name but ``device_standing`` IS core's helper (so
    ``event.abandoned`` and the visibility filter of ``check_target`` come from core)."""
    from orch.host import admission, human

    for name in NAMES - {"judge_events", "device_standing"}:
        assert getattr(relayadmit, name) is getattr(admission, name), name
    assert relayadmit.judge_events is human.judge_events
    if hasattr(admission, "device_standing"):  # pragma: no cover - after #482 merges
        assert list(inspect.signature(admission.device_standing).parameters) == SIGS["device_standing"]


@pytest.mark.parametrize("impl", _impls())
def test_window_edges_in_host_seconds(impl):
    now = 1_790_000_000.0
    impl.check_window(ulid(int(now * 1000) - 900_000), now)
    impl.check_window(ulid(int(now * 1000) + 300_000), now)
    for off in (-900_001, 300_001):
        with pytest.raises(HostError) as e:
            impl.check_window(ulid(int(now * 1000) + off), now)
        assert e.value.code == "event.expired"


@pytest.mark.parametrize("impl", _impls())
def test_known_or_fresh_dedups_by_signed_digest_and_actor_before_the_window(served, impl):  # noqa: F811
    entry = _entry(served)
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    _submit(served, entry, owner, log, ev)
    store, later = served.service.store, served.lab.ws.clock[0] + 10_000
    seq = _on_worker(served, lambda: impl.known_or_fresh(store, log, {**ev, "sig": "other-bytes"}, later))
    assert seq == int(store.logged_event(log, ev["id"])["seq"])
    other = {**ev, "title": "other content"}
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: impl.known_or_fresh(store, log, other, later))
    assert e.value.code == "event.duplicate_id"
    log2, fresh = _new_ticket(served, owner)
    assert _on_worker(served, lambda: impl.known_or_fresh(store, log2, fresh, served.lab.ws.clock[0])) is None
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: impl.known_or_fresh(store, log2, fresh, later))
    assert e.value.code == "event.expired"


@pytest.mark.parametrize("impl", _impls())
def test_check_target_key_rule_carries_next_key_in_detail(served, impl):  # noqa: F811
    owner = served.lab.ws.owner
    store = served.service.store
    want = _next_key(served)
    assert _on_worker(served, lambda: impl.next_key(store)) == want
    prefix, n = want.rsplit("-", 1)
    log, ev = _new_ticket(served, owner, key=f"{prefix}-{int(n) + 3}")
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: impl.check_target(store, log, ev))
    assert e.value.code == "ticket.key_not_next" and e.value.detail == {"next_key": want}
    log3, ev3 = _new_ticket(served, owner)
    other_owner = _event(served, owner, log3, "ticket.created", key=want, ticket_type="bug", title="t",
                         owner="p_" + "9" * 32)  # fmt: skip
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: impl.check_target(store, log3, other_owner))
    assert e.value.code == "invalid.input"
    uid, _ = served.ticket()
    log2, ev2 = _new_ticket(served, owner, log=uid)
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: impl.check_target(store, log2, ev2))
    assert e.value.code == "ticket.exists"
    missing = ulid(int(served.lab.ws.clock[0] * 1000))
    ans = _event(served, owner, missing, "question.answered", question="q1", hash="sha256:" + "0" * 64, option="y")
    with pytest.raises(HostError) as e:
        _on_worker(served, lambda: impl.check_target(store, missing, ans))
    assert e.value.code == "not_found"


@pytest.mark.parametrize("impl", _impls())
def test_signed_digest_ignores_the_signature(served, impl):  # noqa: F811
    owner = served.lab.ws.owner
    log, ev = _new_ticket(served, owner)
    d = impl.signed_digest(served.wid, log, ev)
    assert d.startswith("sha256:") and d == impl.signed_digest(served.wid, log, {**ev, "sig": "x"})
    assert d != impl.signed_digest(served.wid, log, {**ev, "title": "changed"})
