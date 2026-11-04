"""Manual schedule commands are person-only and read the signed ledger."""

from datetime import UTC, datetime

from tests.ops.test_task_ac import claimed


def test_schedule_commands(hws, agent, me, tmp_path):
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
    path = tmp_path / "brief.txt"
    path.write_text("Review the ticket")
    at = datetime.fromtimestamp(hws.clock[0] + 7200, UTC).strftime("%Y-%m-%d %H:%M")
    args = ("schedule", "add", "1", "--at", at, "--brief-file", str(path))
    assert agent.j(*args).err_code == "human_only"
    result = me.j(*args)
    assert result.code == 0, result.out
    sid = result.data["id"]
    _r = me.j("schedule", "list")
    assert _r.code == 0, (_r.out, _r.doc)
    assert _r.data["total"] == 1
    assert agent.j("schedule", "show", sid).data["brief"] == path.read_text()
    assert "No click" in hws.reviews[-1]
    assert me.j("schedule", "cancel", sid).code == 0
    assert hws.view("1").schedules[0]["state"] == "cancelled"
