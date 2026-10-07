"""Run estimates (docs/factory.md, "Estimates"): durations of finished runs, recorded once by the runner, and "about
N minutes left" only from at least three comparable runs. A fake clock and synthetic events; no runner."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from orch import clock
from orch.core import factory_estimates as fe
from orch.core.events import Event
from orch.errors import HumanOnlyError

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def _s(minutes):
    return clock.stamp_s(T0 + timedelta(minutes=minutes))


def _run(n, total, release=None, sizes=("s", "m")):
    """A finished epic E-n whose run took `total` minutes: planner 5, one child per size (10 minutes each)."""
    eid = f"L-{100 + n}"
    kids = [(None, SimpleNamespace(id=f"L-{200 + 10 * n + i}", meta={"size": z})) for i, z in enumerate(sizes)]
    ev = [Event(1, _s(5), k.id, "ticket.created", "agent:x", "cli") for _, k in kids]
    for i, (_, k) in enumerate(kids):
        ev += [Event(2, _s(6 + i), k.id, "claim.taken", "agent:x", "cli"),
               Event(3, _s(16 + i), k.id, "ticket.moved", "agent:x", "cli", {"to": "testing"})]
    if release:
        ev.append(Event(4, _s(total - 2), eid, "release.stage", "human:you", "runner",
                        {"stage": "merge", "proven": True}))
    ev.append(Event(5, _s(total), eid, "verdict.given", "human:you", "dashboard"))
    epic = SimpleNamespace(id=eid, status="done")
    d = {"id": f"sha256:{n}", "at": _s(0), "release": release}
    return epic, d, ev, kids


def test_measure_reads_every_step_from_events(ws):
    epic, d, ev, kids = _run(1, 60, release="merge")
    m = fe.measure(ws, epic, d, ev, kids)
    assert m["total_s"] == 3600 and m["planner_s"] == 300
    assert m["children"] == [["s", 600], ["m", 600]]
    assert m["stages"] == {"merge": (58 - 17) * 60}  # from the last child in testing to the proven merge
    assert fe.measure(ws, SimpleNamespace(id="L-9", status="open"), d, ev, kids) is None


def test_only_the_runner_records_and_only_once(ws, human, agent):
    epic, d, ev, kids = _run(1, 60)
    with pytest.raises(HumanOnlyError):
        fe.record(ws, agent, epic, d, ev, kids)
    assert fe.record(ws, human, epic, d, ev, kids) is True
    assert fe.record(ws, human, epic, d, ev, kids) is False
    assert [r["total_s"] for r in fe.records(ws)] == [3600]


def test_no_number_without_three_comparable_runs(ws, human, monkeypatch):
    running = {"id": "sha256:live", "at": _s(0), "release": None}
    for n, total in enumerate((40, 60), 1):
        fe.record(ws, human, *_run(n, total))
    fe.record(ws, human, *_run(3, 50, release="merge"))  # another release target: not comparable
    assert fe.estimate(ws, running, 600) is None
    fe.record(ws, human, *_run(4, 80))
    assert fe.estimate(ws, running, 600) == {"minutes": 50, "runs": 3}  # median 60 minutes, 10 gone
    assert fe.estimate(ws, running, 59 * 60 + 30) == {"minutes": 1, "runs": 3}
    assert fe.estimate(ws, running, 3600) is None  # past the median: no number is made up


def test_a_damaged_record_is_left_out(ws, human):
    for n, total in enumerate((40, 60, 80), 1):
        fe.record(ws, human, *_run(n, total))
    victim = sorted(fe._dir().iterdir())[0]
    victim.write_text('{"total_s": 1}', encoding="utf-8")
    assert len(fe.records(ws)) == 2
    assert fe.estimate(ws, {"id": "x", "release": None}, 0) is None


def test_the_run_view_says_it_only_with_enough_runs(ws, human, monkeypatch):
    from orch.dashboard.data import factory as data
    d = {"id": "sha256:live", "release": None}
    assert data._estimate(ws, d, 0) is None
    for n, total in enumerate((40, 60, 80), 1):
        fe.record(ws, human, *_run(n, total))
    assert data._estimate(ws, d, 0) == {"minutes": 60, "runs": 3}
    monkeypatch.setattr(fe, "records", lambda ws: (_ for _ in ()).throw(OSError("x")))
    assert data._estimate(ws, d, 0) is None


def test_the_records_folder_is_guarded():
    from orch.hooks import guard
    assert "durations" in guard.PERMIT_NAMES
