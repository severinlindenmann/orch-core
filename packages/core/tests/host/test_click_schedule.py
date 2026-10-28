"""Run 13 (owner e2e, blocking): a manual schedule made in the dashboard (Schedules > New schedule) is a click
(``schedule.add`` emits ``schedule.set``). The request the host prepares must pass the click signer's own checks and
View, and sign, exactly as the dashboard sends it: a once-schedule 1.5 h ahead, no ``run_hours`` (the host default)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from orch.click import view as views
from orch.click.protocol import RISK
from orch.ops.schedule import RISK as SCHEDULE_RISK
from tests.host import human_helpers
from tests.host.test_click import Click

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def click(served):
    c = Click(served)
    yield c
    c.close()


def once_at(served, ahead: int) -> str:
    """The dashboard's once ``at``: local ``YYYY-MM-DD HH:MM`` in ``tz`` (UTC here), ``ahead`` seconds from now."""
    due = int(served.lab.ws.clock[0] + ahead) // 60 * 60
    return datetime.fromtimestamp(due, UTC).strftime("%Y-%m-%d %H:%M")


def schedule_args(served, key, **over):
    return {"ref": key, "brief": "Write the weekly report", "at": once_at(served, 5400), "tz": "UTC", **over}


@pytest.mark.parametrize("extra", [{}, {"run_hours": 2}, {"every": "day", "at": "09:30"}])
def test_a_schedule_set_prepared_by_the_host_is_accepted_and_signed_by_the_click_signer(click, served, human, extra):
    human.run("addon.grant", {"name": "schedules"})
    uid, key = human.ticket()
    p = click.prepare("schedule.add", schedule_args(served, key, **extra))
    assert p["confirm"] == "click"
    prepared = click.signer.approvals[(served.wid, p["request_id"])].prepared
    assert [e["type"] for e in prepared["events"]] == ["schedule.set"]
    common = dict(
        workspace_id=served.wid, person=served.lab.ws.owner.ref, device=served.lab.ws.owner.device, files=None
    )
    assert views.check_prepared(prepared, **common) == "click"
    v = click.view(p["request_id"])
    shown = v["view"]
    assert shown["op"] == "schedule.add"
    # the convenience-signing notice every click View carries (the dashboard server refuses a View without it); the
    # schedule's own risk is a line of the review
    assert shown["risk"] == RISK
    assert SCHEDULE_RISK in shown["lines"] and "No click or confirmation is required at firing time." in shown["lines"]
    # FIRST, before the rule's details: the dashboard shows `lines` in a short scroll box (ClickSignPanel max-h-64)
    assert shown["lines"][:2] == [SCHEDULE_RISK, "No click or confirmation is required at firing time."]
    assert shown["lines"].count(SCHEDULE_RISK) == 1
    click.mono.t += 1.5
    res = click.sign(p["request_id"], v["view_digest"])
    assert res["result"] and click.signatures == 1
