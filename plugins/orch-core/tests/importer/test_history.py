"""The v1 ticket and its history travel as one hashed, read-only artifact per ticket."""

from __future__ import annotations

import json

from orch import canon
from tests.importer.helpers import marker
from tests.ops.humans import agent, hws, me  # noqa: F401


def test_the_marker_holds_the_v1_file_and_the_ticket_history(imported, hws, v1):
    m = marker(hws, "DEMO-0003")
    src = next((v1 / "orchestrator" / "tickets" / "in-progress").glob("DEMO-0003*")).read_bytes()
    assert m["schema"] == "orch.import.v1/1" and m["v1_key"] == "DEMO-0003" and m["v1_status"] == "in-progress"
    assert m["v1_file"].encode() == src and m["v1_file_sha256"] == canon.artifact_digest(src)
    assert [e["kind"] for e in m["events"]] == ["ticket.created", "claim.taken", "gate.approved"]
    assert m["events"][2]["data"] == {"gate": "requirements"}  # v1's approval is history, not a v2 decision
    assert any("sections only in the history" in x and "Log" in x for x in m["not_imported"])
    assert any("verify lines" in x for x in m["not_imported"])


def test_only_the_tickets_own_events_are_kept(imported, hws):
    assert [e["kind"] for e in marker(hws, "DEMO-0008")["events"]] == ["question.asked"]
    assert marker(hws, "DEMO-0004")["events"] == []


def test_the_marker_is_bound_by_its_digest_in_the_log(imported, hws):
    uid = hws.uid("DEMO-0003")
    data = (hws.root / "tickets" / uid / "artifacts" / "v1-import.json").read_bytes()
    ev = next(e for e in hws.events("DEMO-0003") if e["type"] == "artifact.added" and e["name"] == "v1-import.json")
    assert ev["sha256"] == canon.artifact_digest(data) and ev["bytes"] == len(data) and ev["kind"] == "other"
    assert json.loads(data)["note"].startswith("A read-only copy")


def test_every_person_event_is_the_importers_signature(imported, hws):
    for key in ("DEMO-0001", "DEMO-0005"):
        for e in hws.events(key):
            assert e["actor"]["kind"] == "person" and e["actor"]["id"] == hws.owner.ref and e["sig"]


def test_hostile_text_is_cleaned_and_counted(imported, hws):
    body = (hws.root / "tickets" / hws.uid("DEMO-0004") / "body.md").read_text()
    assert "\x1b" not in body and "‮" not in body and "�" in body
    assert "red" in body
    assert marker(hws, "DEMO-0004")["v1_file"].count("\x1b") == 1  # the original stays in the history


def test_an_agent_session_and_an_approval_work_on_an_imported_ticket(imported, hws, agent, me):  # noqa: F811
    key = "DEMO-0003"
    assert agent("claim", key).code == 0
    r = agent("task", "start", "T1")
    assert r.code == 0, r.err
    assert agent("log", "-m", "working on the import").code == 0
    assert agent("release").code == 0
    r = me("approve", "requirements", "--ref", key)
    assert r.code == 0, r.err + r.out
    s = hws.other()
    try:
        assert s.chain_errors() == []
        v = s.ticket(key)
        assert v.gates["requirements"].decisions and v.status == "open"
    finally:
        s.close()


def test_doctor_and_check_are_clean_after_an_import_and_a_session(imported, hws, agent, me):  # noqa: F811
    from orch.instructions import write_workspace_files

    write_workspace_files(hws.root)
    assert agent("claim", "DEMO-0003").code == 0
    assert agent("log", "-m", "after the import").code == 0
    assert agent("release").code == 0
    assert me("approve", "requirements", "--ref", "DEMO-0003").code == 0
    for cmd in ("doctor", "check"):
        r = me.j(cmd)
        assert r.code == 0 and r.data["problems"] == 0, r.out


def test_imported_tickets_get_no_special_edit_rights(imported, hws, agent):  # noqa: F811
    """Import is a person's signed act; afterwards an imported ticket is an ordinary one: an agent still cannot set
    protected fields, and nothing it does can approve a gate."""
    assert agent("claim", "DEMO-0003").code == 0
    for kv in ("key=DEMO-0999", "visibility=none"):
        assert agent("set", kv).code != 0
    v = hws.view("DEMO-0003")
    assert v.key == "DEMO-0003" and v.visibility == "workspace"
    assert all(not g.decisions for g in v.gates.values())
