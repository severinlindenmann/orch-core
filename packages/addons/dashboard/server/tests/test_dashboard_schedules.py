"""The "My schedules" tab (#720): the read of the schedules, and the two signed actions that change them."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orch_dashboard import schedules, signing
from orch_dashboard.errors import HostError

ME = "p_" + "a" * 32
OTHER = "p_" + "b" * 32
SID = "sc_" + "0" * 26
KEY = "ORCH-1"
WHEN = 1_900_000_000


class FakeGateway:
    def __init__(self, role="member", addon=True, rows=None, prefix="DEMO"):
        self.addon = addon
        self.prefix = prefix
        self.rows = rows if rows is not None else []
        self.calls = []
        self._link = SimpleNamespace(welcome={"peer": {"person": ME, "role": role}, "ops": {}})

    def enabled(self, ws):
        return self._link

    def link(self, ws):
        return self._link

    def addon_on(self, link, name):
        return self.addon and name == "schedules"

    def call(self, ws, op, args):
        self.calls.append((op, args))
        if op == "schedule.list":
            return {"rows": self.rows, "total": len(self.rows)}
        if op == "workspace.show":
            return {"prefix": self.prefix}
        if op == "ticket.get":
            if args["ref"] == "ORCH-404":
                raise HostError({"code": "not_found", "message": "no"}, exit=2)
            return {"doc": {"key": args["ref"], "title": "Weekly report"}}
        raise AssertionError(op)


def row(**over):
    base = {
        "id": SID,
        "key": KEY,
        "person": ME,
        "rule": {"every": "week", "at": "09:00", "tz": "UTC", "weekdays": [0]},
        "starts": "2030-01-01T00:00:00Z",
        "ends": None,
        "brief": "Write the report\x00\u202e",
        "run_hours": 2,
        "label": "report",
        "state": "live",
        "occurrences": 0,
    }
    return base | over


@pytest.fixture(autouse=True)
def fixed_next(monkeypatch):
    afters = []

    def fake(rule, starts, ends, after):
        afters.append(after)
        return WHEN

    monkeypatch.setattr("orch.model.schedules.occurrences", fake)
    return afters


def test_off_without_the_addon():
    out = schedules.build(FakeGateway(addon=False, rows=[row()]), "w")
    assert out["available"] is False and out["rows"] == []


def test_rows_for_a_member():
    out = schedules.build(FakeGateway(rows=[row(), row(id="sc_" + "1" * 26, person=OTHER)]), "w", now=1)
    assert out["available"] is True and out["can"] == {"add": True, "cancel": True}
    mine, theirs = out["rows"]
    assert mine["ticket"] == KEY and mine["title"] == "Weekly report" and mine["label"] == "report"
    assert mine["brief"] == "Write the report"
    assert mine["next_run"] == datetime.fromtimestamp(WHEN, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert mine["mine"] is True and mine["can_cancel"] is True
    assert theirs["mine"] is False and theirs["can_cancel"] is False


def test_an_owner_cancels_anyone_s():
    out = schedules.build(FakeGateway(role="owner", rows=[row(person=OTHER)]), "w")
    assert out["rows"][0]["mine"] is False and out["rows"][0]["can_cancel"] is True


def test_a_maintainer_cancels_only_their_own():
    # The model lets the setter or an owner cancel (schedules_wire.cancelled_event); the page agrees.
    out = schedules.build(FakeGateway(role="maintainer", rows=[row(person=OTHER), row(id="sc_" + "1" * 26)]), "w")
    theirs, mine = out["rows"]
    assert theirs["can_cancel"] is False and mine["can_cancel"] is True


def test_rows_carry_run_hours_and_a_plain_label():
    out = schedules.build(FakeGateway(rows=[row(label="rep\u202eort"), row(run_hours="8", label=None)]), "w")
    first, second = out["rows"]
    assert first["run_hours"] == 2 and first["label"] == "report"
    assert second["run_hours"] is None and second["label"] is None


def test_a_viewer_cannot_add():
    out = schedules.build(FakeGateway(role="viewer", rows=[row()]), "w")
    assert out["can"] == {"add": False, "cancel": False}


def test_no_next_run_unless_live():
    out = schedules.build(FakeGateway(rows=[row(state="cancelled"), row(state="expired")]), "w")
    assert [r["next_run"] for r in out["rows"]] == [None, None]


def _epoch(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


def test_next_run_never_precedes_first_allowed_or_a_recorded_occurrence(fixed_next):
    from orch.model import followups as fu

    start = _epoch("2030-01-01T00:00:00Z")
    # signed at `starts`: the first allowed instant is set_at plus the minimum lead; a dev sandbox has a shorter lead
    schedules.build(FakeGateway(rows=[row(set_at="2030-01-01T00:00:00Z")]), "w", now=1)
    assert fixed_next[-1] == start + fu.FU_MIN_LEAD_S - 1
    schedules.build(FakeGateway(rows=[row(set_at="2030-01-01T00:00:00Z")], prefix="SBX"), "w", now=1)
    assert fixed_next[-1] == start + fu.DEV_MIN_LEAD_S - 1
    # now and the latest recorded occurrence (fired or settled) both push it later
    schedules.build(FakeGateway(rows=[row(set_at="2030-01-01T00:00:00Z")]), "w", now=start + 10**6)
    assert fixed_next[-1] == start + 10**6
    held = {"2031-01-01T00:00:00Z": {"fired_at": "x"}, "2030-06-01T00:00:00Z": {"outcome": "ran"}}
    schedules.build(FakeGateway(rows=[row(set_at="2030-01-01T00:00:00Z", occurrences=held)]), "w", now=1)
    assert fixed_next[-1] == _epoch("2031-01-01T00:00:00Z")


def test_the_minimum_lead_of_a_new_schedule_is_cores(monkeypatch):
    """Run 13: the form offers no time before now plus the lead the host enforces (`schedule.denied` below it), read
    from core's caps of this workspace: an hour, a dev sandbox's shorter one; also with no schedule yet."""
    from orch.model import followups as fu

    assert schedules.build(FakeGateway(), "w")["min_lead_s"] == fu.FU_MIN_LEAD_S == 3600
    assert schedules.build(FakeGateway(prefix="SBX"), "w")["min_lead_s"] == fu.DEV_MIN_LEAD_S
    assert schedules.build(FakeGateway(addon=False), "w")["min_lead_s"] is None
    unread = FakeGateway()
    monkeypatch.setattr(unread, "call", lambda ws, op, args: {"rows": []} if op == "schedule.list" else {})
    assert schedules.build(unread, "w")["min_lead_s"] == fu.FU_MIN_LEAD_S  # no prefix read: the real lead


def test_the_schedules_read_is_served_and_is_a_host_read():
    """Run 13: the route was never registered and `schedule.list` was not a read of the host link (My schedules got a
    404)."""
    from conftest import PORT

    from orch_dashboard.app import create_app
    from orch_dashboard.hostlink import OPS

    assert "/api/w/{ws}/schedules" in {route.path for route in create_app(PORT).routes}
    assert "schedule.list" in OPS


def test_next_run_is_null_when_nothing_is_left(monkeypatch):
    monkeypatch.setattr("orch.model.schedules.occurrences", lambda *a: None)
    assert schedules.build(FakeGateway(rows=[row()]), "w")["rows"][0]["next_run"] is None


def test_last_outcome_with_a_reason_only_from_the_closed_list():
    outcomes = [
        {"occurrence": "2030-01-02T09:00:00Z", "outcome": "ran", "reason": None},
        {"occurrence": "2030-01-03T09:00:00Z", "outcome": "did_not_run", "reason": "busy"},
    ]
    out = schedules.build(
        FakeGateway(rows=[row(outcomes=outcomes), row(outcomes=[{**outcomes[1], "reason": "<b>"}])]), "w"
    )
    first, second = out["rows"]
    assert first["last_outcome"] == {"at": "2030-01-03T09:00:00Z", "outcome": "did_not_run", "reason": "busy"}
    assert second["last_outcome"]["reason"] is None
    held = {"2030-01-02T09:00:00Z": {"outcome": "failed", "reason": "harness_failed"}, "2030-01-04T09:00:00Z": {}}
    out = schedules.build(FakeGateway(rows=[row(occurrences=held), row()]), "w")
    assert out["rows"][0]["last_outcome"] == {
        "at": "2030-01-02T09:00:00Z",
        "outcome": "failed",
        "reason": "harness_failed",
    }
    assert out["rows"][1]["last_outcome"] is None


def test_max_run_hours_is_capped_by_the_setters_grant_hours():
    rows = [row(grant_hours_cap=3, run_hours=8), row(grant_hours_cap=8, run_hours=2), row(run_hours=5)]
    out = schedules.build(FakeGateway(rows=rows), "w")
    assert [r["max_run_hours"] for r in out["rows"]] == [3, 2, 5]
    assert out["grant_hours_cap"] == 3
    assert schedules.build(FakeGateway(rows=[row(person=OTHER, grant_hours_cap=2)]), "w")["grant_hours_cap"] is None


def test_unreadable_ticket_has_no_title():
    out = schedules.build(FakeGateway(rows=[row(key="ORCH-404")]), "w")
    assert out["rows"][0]["title"] is None


def add(**over):
    base = {
        "action": "schedule_add",
        "key": KEY,
        "brief": "Write the report",
        "at": "09:00",
        "every": "week",
        "tz": "Europe/Berlin",
        "weekdays": [2, 0],
    }
    return base | over


def test_add_maps_to_the_host_op():
    body = signing.ScheduleAdd(**add(run_hours=2))
    op, args = signing.host_call(body)
    assert op == "schedule.add"
    assert args == {
        "ref": KEY,
        "brief": "Write the report",
        "at": "09:00",
        "every": "week",
        "tz": "Europe/Berlin",
        "weekdays": "0,2",
        "run_hours": 2,
    }


def test_once_has_a_date_and_no_every():
    body = signing.ScheduleAdd(**add(every=None, weekdays=None, at="2030-02-03 08:30"))
    _, args = signing.host_call(body)
    assert "every" not in args and args["at"] == "2030-02-03 08:30"


@pytest.mark.parametrize(
    "over",
    [
        {"at": "2030-02-03 08:30"},  # recurring needs HH:MM
        {"at": "24:00"},
        {"every": None, "weekdays": None, "at": "09:00"},  # once needs a date
        {"every": None, "weekdays": None, "at": "2030-02-30 08:30"},  # not a day
        {"weekdays": [7]},
        {"every": "day", "weekdays": [1]},  # weekdays are for weekly
        {"every": "week", "weekdays": None, "day": 3},  # a day of the month is for monthly
        {"every": "month", "weekdays": None, "day": 32},
        {"run_hours": 9},
        {"run_hours": 0},
        {"brief": ""},
        {"brief": "x" * 4097},
        {"extra": 1},
    ],
)
def test_add_refuses(over):
    with pytest.raises(ValidationError):
        signing.ScheduleAdd(**add(**over))


def test_add_brief_limit_is_inclusive():
    signing.ScheduleAdd(**add(brief="x" * 4096))


def test_cancel_maps_to_the_host_op():
    body = signing.ScheduleCancel(action="schedule_cancel", key=KEY, id=SID)
    assert signing.host_call(body) == ("schedule.cancel", {"id": SID})


@pytest.mark.parametrize("bad", ["sc_8" + "0" * 25, "fu_" + "0" * 26, "sc_" + "0" * 25, "sc_" + "I" * 26])
def test_cancel_refuses_other_ids(bad):
    with pytest.raises(ValidationError):
        signing.ScheduleCancel(action="schedule_cancel", key=KEY, id=bad)


def test_expected_events():
    cancel = signing.ScheduleCancel(action="schedule_cancel", key=KEY, id=SID)
    check = signing.expected(cancel)
    assert check([{"type": "schedule.cancelled", "schedule": SID}])
    assert not check([{"type": "schedule.cancelled", "schedule": "sc_" + "1" * 26}])
    assert not check([{"type": "schedule.set", "schedule": SID}])
    assert not check([])
    check = signing.expected(signing.ScheduleAdd(**add()))
    assert check([{"type": "schedule.set", "schedule": SID}])
    assert not check([{"type": "schedule.cancelled", "schedule": SID}])
    assert not check([{"type": "schedule.set"}, {"type": "schedule.set"}])


def test_plan_cancel_needs_the_schedule_on_that_ticket():
    body = signing.ScheduleCancel(action="schedule_cancel", key=KEY, id=SID)
    planned = signing.plan(FakeGateway(rows=[row()]), "w", body)
    assert (planned.op, planned.args, planned.person) == ("schedule.cancel", {"id": SID}, ME)
    for rows in ([], [row(key="ORCH-2")]):
        with pytest.raises(HostError) as err:
            signing.plan(FakeGateway(rows=rows), "w", body)
        assert err.value.error["code"] == "not_found"


def test_plan_add_has_no_base():
    planned = signing.plan(FakeGateway(), "w", signing.ScheduleAdd(**add()))
    assert planned.op == "schedule.add" and planned.base is None


def test_schedule_new_shape_passthrough():
    body = add(target="new", title="Mail")
    body.pop("key")
    action = signing.ScheduleAdd(**body)
    assert action.props()["target"] == "new"
    assert action.props()["title"] == "Mail"
    assert "ref" not in action.props()
    check = signing.expected(action, ME)
    anchor = {"type": "ticket.created", "ticket_type": "chore", "title": "Mail", "owner": ME, "schedule": SID}
    rule = {"type": "schedule.set", "schedule": SID, "target": "new", "title": "Mail"}
    assert check([anchor, rule])
    # the host must send exactly the anchor, then the schedule: nothing else, another order or other values is refused
    assert not check([rule])
    assert not check([rule, anchor])
    assert not check([anchor, rule, rule])
    assert not check([{**anchor, "title": "Other"}, rule])
    assert not check([{**anchor, "owner": OTHER}, rule])
    assert not check([{**anchor, "ticket_type": "feature"}, rule])
    assert not check([{**anchor, "schedule": "sc_" + "1" * 26}, rule])
    assert not check([{**anchor, "via": {"anchor": "x"}}, rule])
    assert not check([anchor, {**rule, "title": "Other"}])
    assert not check([anchor, {"type": "schedule.set", "schedule": SID}])
    assert not signing.expected(signing.ScheduleAdd(**add()))([{"type": "schedule.set", "target": "unknown"}])


@pytest.mark.parametrize(
    "over", [{"target": "unknown"}, {"title": "Mail"}, {"target": "new"}, {"target": "new", "title": "a\u200bb"}]
)
def test_schedule_invalid_target_title(over):
    with pytest.raises(ValueError):
        signing.ScheduleAdd(**add(**over))


@pytest.mark.parametrize("target", ["unknown", None, 5])
def test_one_unsupported_row_never_breaks_the_page(target):
    other = "sc_" + "1" * 26
    data = schedules.build(FakeGateway(rows=[row(target=target, title="Mail"), row(id=other)]), "w")
    by_id = {r["id"]: r for r in data["rows"]}
    assert by_id[SID]["supported"] is False and by_id[SID]["note"] == "Needs a newer dashboard"
    assert by_id[SID]["can_cancel"] is False
    assert by_id[other]["ticket"] == KEY and "supported" not in by_id[other]


def test_last_outcome_keeps_new_child_results():
    for outcome, reason in (("ran_nothing", None), ("did_not_run", "skipped_open_child")):
        recorded = {"occurrence": "2030-01-03T09:00:00Z", "outcome": outcome, "reason": reason}
        assert schedules.last_outcome({"outcomes": [recorded]}) == {
            "at": recorded["occurrence"],
            "outcome": outcome,
            "reason": reason,
        }
