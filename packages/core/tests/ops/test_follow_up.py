"""Follow-up operations through the CLI and real ledger."""

from datetime import datetime

import pytest

from orch.ops.followup import due
from tests.ops.helpers import OTHER, fenced
from tests.ops.test_task_ac import claimed


@pytest.fixture
def schedules(ws):
    ws.store.append(
        ws.person_event(
            ws.owner,
            "workspace",
            "addon.granted",
            name="schedules",
            version="0.1.0",
            package_sha256="sha256:" + "0" * 64,
            capabilities=["spawn_agent"],
            fields={},
            sections=[],
            artifact_kinds=[],
        ),
        log="workspace",
    )


def test_set_releases_claim_and_show_fences_brief(ws, cli, schedules, tmp_path):
    claimed(cli)
    path = tmp_path / "brief.md"
    path.write_text("Check the build\nthen continue.")
    r = cli.j("follow-up", "--in", "2d", "--brief-file", str(path), "--label", "build")
    assert r.code == 0, r.out
    assert r.data["state"] == "pending" and r.data["label"] == "build"
    assert r.data["due"] == r.data["deadline"]
    assert ws.view("1").claim is None
    assert "end your session" in str(r.doc)
    shown = cli("follow-up", "show", r.data["id"])
    assert shown.code == 0, shown.err
    assert fenced(shown.out) == path.read_text()
    assert "follow-up brief" in shown.out and "set by:" in shown.out
    assert cli.j("follow-up", "show", r.data["id"]).data["label"] == "build"


@pytest.mark.parametrize(
    "duration,brief,code",
    [
        ("59m", "later", "too_soon"),
        ("15d", "later", "too_far"),
        ("2d", "", "bad_brief"),
        ("2d", "é" * 2049, "bad_brief"),
    ],
    ids=["too-soon", "too-far", "empty-brief", "byte-limit"],
)
def test_refusals(ws, cli, schedules, duration, brief, code):
    claimed(cli)
    r = cli.j("follow-up", "--in", duration, "--brief", brief)
    assert r.err_code == "followup." + code, r.out
    assert ws.view("1").claim is not None
    assert not ws.view("1").followups


def test_model_denial(cli, schedules):
    claimed(cli)
    r = cli.j("follow-up", "1", "--in", "2d", "--brief", "later", session=OTHER)
    assert r.err_code == "followup.denied", r.out
    assert "not_holder" in r.out


def test_off(cli):
    claimed(cli)
    for args in [("--in", "2d", "--brief", "later"), ("list",), ("show", "fu_missing"), ("cancel", "1")]:
        r = cli.j("follow-up", *args)
        assert r.err_code == "addon.off", r.out


def test_list_and_cancel(ws, cli, schedules):
    claimed(cli)
    first = cli.j("follow-up", "--in", "2d", "--brief", "first").data["id"]
    cli("new", "Second")
    cli("claim", "2")
    second = cli.j("follow-up", "--in", "90m", "--brief", "second").data["id"]
    assert [r["id"] for r in cli.j("follow-up", "list").data["rows"]] == [second, first]
    assert cli.j("follow-up", "list", "1").data["total"] == 1
    assert cli.j("follow-up", "cancel", first, "--reason", "no longer needed").data["reason"] == "agent"
    assert cli.j("follow-up", "cancel", "2", "--reason", "dismissed").data["reason"] == "dismissed"
    assert cli.j("follow-up", "list").data["total"] == 0
    assert cli.j("follow-up", "list", "--all").data["total"] == 2
    assert cli.j("follow-up", "show", first).data["state"] == "cancelled"
    assert ws.events("1")[-1]["type"] == "followup.cancelled"


def test_local_clock_parsing():
    now = datetime(2026, 10, 26, 12, 0).timestamp()
    assert due({"at": "+2d 09:00"}, now) == due({"at": "2026-10-28T09:00"}, now)
    assert due({"at": "2026-10-28T09:00:00"}, now) == due({"at": "+2d 09:00"}, now)


def test_set_text(cli, schedules):
    claimed(cli)
    r = cli("follow-up", "--in", "36h", "--brief", "check later")
    assert r.code == 0, r.err
    assert r.first.startswith("ok DEMO-0001 followup.set fu_"), r.out
    assert "due=" in r.first and "state=pending" in r.first
    assert "orch follow-up show fu_" in r.out and "end your session" in r.out


def test_narrow_grant(cli, ws, schedules):
    grant = ws.narrow_grant(["new", "claim", "follow_up", "follow_up.cancel"])
    from tests.ops.helpers import Cli

    agent = Cli(ws, ORCH_GRANT=grant)
    claimed(agent)
    result = agent.j("follow-up", "--in", "2d", "--brief", "later")
    assert result.code == 0, result.out
    assert agent.j("follow-up", "cancel", result.data["id"]).code == 0


def test_show_derived_cancellation(cli, schedules):
    claimed(cli)
    fid = cli.j("follow-up", "--in", "2d", "--brief", "later").data["id"]
    assert cli("claim", "1", session=OTHER).code == 0
    rec = cli.j("follow-up", "show", fid).data
    assert (rec["state"], rec["reason"]) == ("cancelled", "claimed_by_other")
    assert cli.j("follow-up", "list").data["total"] == 0


def test_all_keeps_only_recent_terminal_records(ws, cli, anon, schedules):
    claimed(cli)
    fid = cli.j("follow-up", "--in", "2d", "--brief", "later").data["id"]
    cli("follow-up", "cancel", fid)
    ws.tick(31 * 86400)
    assert anon.j("follow-up", "list", "--all").data["total"] == 0
    assert anon.j("follow-up", "show", fid).data["state"] == "cancelled"


def test_on_event_and_webhook_follow_ups(ws, cli, schedules):
    claimed(cli)
    r = cli.j("follow-up", "--in", "2d", "--on", "question.answered:q1", "--brief", "after the answer")
    assert r.code == 0, r.out
    assert r.data["trigger"] == "orch" and r.data["state"] == "pending"
    rec = ws.view("1").followups[0]
    assert rec["on"]["ref"] == "q1" and rec["due"] is None and rec["deadline"]
    shown = cli("follow-up", "show", r.data["id"])
    assert "waiting for: question.answered:q1" in shown.out
    assert "trigger=orch" in cli("follow-up", "list").out


def test_on_bad_and_addon_values(cli, schedules):
    claimed(cli)
    assert cli.j("follow-up", "--in", "2d", "--on", "nope.nothing", "--brief", "x").err_code == "invalid.input"
    assert cli.j("follow-up", "--in", "2d", "--on", "webhook:hk_short", "--brief", "x").err_code == "invalid.input"


def test_webhook_new_and_bind(ws, cli, schedules):
    from orch.model import followup_events as fe

    claimed(cli)
    r = cli.j("webhook", "new", "1", "--label", "backfill done")
    assert r.err_code == "followup.no_receiver", r.out  # no receiver on this host
    cli.env_kw["ORCH_HOOK_BASE"] = "https://hooks.example:9000/"
    r = cli.j("webhook", "new", "1", "--label", "backfill done")
    assert r.code == 0, r.out
    hid = r.data["id"]
    assert r.data["url"] == f"https://hooks.example:9000/h/{hid}"
    bound = cli.j("follow-up", "--in", "2d", "--on", f"webhook:{hid}", "--brief", "after the ring")
    assert bound.code == 0 and bound.data["trigger"] == "webhook", bound.out
    rec = ws.view("1").followups[0]
    assert rec["hook_prefix"] == fe.hook_hash(hid, ws.store.workspace_id)[:8]
    assert "hook" not in rec["on"] and "hook" not in rec  # the hash is not shown to readers
    assert hid not in str(ws.events("1")[-1])  # only the hash is in the ledger


@pytest.mark.parametrize("event_trigger", [False, True])
def test_human_reschedule_and_undo(hws, agent, me, event_trigger):
    from datetime import UTC

    hws.store.append(
        hws.person_event(
            hws.owner,
            "workspace",
            "addon.granted",
            name="schedules",
            version="0.1.0",
            package_sha256="sha256:" + "0" * 64,
            capabilities=[],
            fields={},
            sections=[],
            artifact_kinds=[],
        ),
        log="workspace",
    )
    claimed(agent)
    extra = ("--on", "question.answered:q1") if event_trigger else ()
    result = agent.j("follow-up", "--in", "2d", "--brief", "later", *extra)
    assert result.code == 0, result.out
    fid = result.data["id"]
    when = datetime.fromtimestamp(hws.clock[0] + 3 * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert agent.j("follow-up", "reschedule", fid, "--when", when).err_code == "human_only"
    result = me.j("follow-up", "reschedule", fid, "--when", when)
    assert result.code == 0, result.out
    field = "deadline" if event_trigger else "due"
    assert hws.events("1")[-1][field] == when
    assert field in hws.reviews[-1]
    assert me.j("follow-up", "reschedule", fid, "--when", "tomorrow").err_code == "followup.denied"
    from orch import canon

    hws.store.append(
        hws.person_event(
            hws.owner,
            hws.view("1").uid,
            "followup.cancelled",
            followup=fid,
            reason="person",
            based_on=canon.event_head(hws.events("1")[-1]),
        ),
        log=hws.view("1").uid,
    )
    assert agent.j("follow-up", "undo", fid).err_code == "human_only"
    result = me.j("follow-up", "undo", fid)
    assert result.code == 0, result.out
    assert hws.view("1").followups[0]["state"] == "pending"
    assert hws.events("1")[-1]["type"] == "followup.restored"
