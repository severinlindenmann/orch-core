# ruff: noqa: F811 (shared pytest fixtures are also function arguments)

"""W30b: show adds the latest relevant failed agent start to its read answer."""

import pytest

from tests.host.human_helpers import Human, ok
from tests.host.test_reads_ticket import w  # noqa: F401

MESSAGE = "The agent program could not be started: claude was not found."
HINT = "Ask the person who set up this computer to check the installation."
FAILURE = {"stop_cause": "program_not_found", "stop_message": MESSAGE, "stop_hint": HINT}
LINE = f"Last agent start failed: {MESSAGE} {HINT}"


def failed(w, key, **overrides):
    rec = {
        "run": "failed",
        "workspace_id": w.wid,
        "kind": "agent",
        "ticket": key,
        "ended_ts": w.lab.ws.clock[0] + 1,
        **FAILURE,
        **overrides,
        "tail": "SECRET RAW OUTPUT",
        "exit": {"error": "SECRET RAW ERROR"},
    }
    w.service.runners.store.records[rec["run"]] = rec
    return rec


def show(w, key):
    return ok(w.client().req("show", {"ref": key}))


def test_show_read_latest_failed_start(w):
    uid, key = w.ticket()
    before = w.lab.ws.read_events(uid)
    failed(w, key, run="older", ended_ts=w.lab.ws.clock[0], stop_message="Older failure.")
    failed(w, key)
    result = show(w, key)
    assert result["data"]["ticket"]["last_agent_start_failure"] == FAILURE
    assert LINE in result["lines"]
    assert "SECRET RAW" not in str(result)
    assert w.lab.ws.read_events(uid) == before


@pytest.mark.parametrize("activity", ["claim", "log"])
def test_show_hides_failure_after_agent_activity(w, activity):
    _, key = w.ticket()
    failed(w, key)
    w.lab.ws.tick(3)
    if activity == "claim":
        Human(w).agent("claim", {"ref": key})
    else:
        w.write(key)
    assert "last_agent_start_failure" not in show(w, key)["data"]["ticket"]


@pytest.mark.parametrize("offset,shown", [(0, False), (1, True)])
def test_show_failure_must_be_strictly_newer_than_claim(w, offset, shown):
    _, key = w.ticket()
    Human(w).agent("claim", {"ref": key})
    failed(w, key, ended_ts=w.lab.ws.clock[0] + offset)
    assert ("last_agent_start_failure" in show(w, key)["data"]["ticket"]) is shown


@pytest.mark.parametrize("overrides", [{"ticket": "OTHER-0001"}, {"kind": "shell"}, {"workspace_id": "other"}])
def test_show_does_not_borrow_another_runs_failure(w, overrides):
    _, key = w.ticket()
    failed(w, key, **overrides)
    assert "last_agent_start_failure" not in show(w, key)["data"]["ticket"]
