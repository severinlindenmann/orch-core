import json
from pathlib import Path

import jsonschema
import pytest

from orch.cli import run
from orch.core import query, store
from orch.core.constants import PRIORITIES, SECTIONS, SIZES, STATUSES, TYPES
from orch.core.questions import question_hash
from orch.core.schema import (SCHEMA_VERSION, example_document, ticket_document, ticket_from_document,
                              ticket_schema)
from orch.core.tasks import FORMAT

ROOT = Path(__file__).resolve().parents[1]
TASKS_SCHEMA = json.loads((ROOT / "docs" / "tasks-view.schema.json").read_text(encoding="utf-8"))
FIXTURE = (Path(__file__).parent / "fixtures" / "tasks-v1.md").read_text(encoding="utf-8")


def _validate(doc):
    jsonschema.Draft202012Validator.check_schema(ticket_schema())
    jsonschema.Draft202012Validator(ticket_schema()).validate(json.loads(json.dumps(doc)))


def test_version_is_semver_major_1():
    import re
    assert re.fullmatch(r"1\.\d+\.\d+", SCHEMA_VERSION)


def test_schema_enums_come_from_the_core_model():
    s = ticket_schema()
    props = s["properties"]
    assert props["status"]["enum"] == list(STATUSES)
    assert props["type"]["enum"] == list(TYPES)
    assert props["priority"]["enum"] == list(PRIORITIES)
    assert props["size"]["enum"] == list(SIZES)
    assert set(props["sections"]["properties"]) == set(SECTIONS)
    assert props["tasks"]["properties"]["format"]["const"] == FORMAT
    assert props["schema_version"]["pattern"] == r"^1\.\d+\.\d+$"


def test_task_list_is_the_mc2t_schema_not_a_copy():
    s = ticket_schema()
    assert s["properties"]["tasks"]["$ref"] == TASKS_SCHEMA["$id"]
    assert s["$defs"]["tasks_view"] == TASKS_SCHEMA


def test_example_validates_and_carries_tasks_and_hashes():
    doc = example_document()
    jsonschema.validate(doc, ticket_schema())
    _validate(doc)
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["tasks"]["format"] == FORMAT and doc["tasks"]["tasks"]
    jsonschema.Draft202012Validator(TASKS_SCHEMA).validate(doc["tasks"])
    q = doc["questions"][0]
    assert q["hash"] == question_hash(q)
    assert doc["gates"]["requirements"]["hash"].startswith("sha256:")


def test_example_is_deterministic():
    assert example_document() == example_document()


def test_golden_task_fixture_validates_inside_a_ticket_document(ws, put):
    tid = put("in-progress", sections={"Tasks": FIXTURE})
    doc = ticket_document(ws, store.load(ws, tid)[1])
    assert doc["tasks"]["error"] is None and len(doc["tasks"]["tasks"]) == 8
    _validate(doc)


def test_a_bad_task_list_fails_ticket_validation(ws, put):
    tid = put("in-progress", sections={"Tasks": FIXTURE})
    doc = json.loads(json.dumps(ticket_document(ws, store.load(ws, tid)[1])))
    doc["tasks"]["tasks"][0]["state"] = "finished"
    with pytest.raises(jsonschema.ValidationError):
        _validate(doc)


def test_document_of_a_real_ticket_validates(ws, aops, hops):
    t = aops.new("Export the meter readings", ask="CSV please")
    aops.ask(t.id, [{"text": "Which delimiter?", "options": [";", ","], "recommended": "A"}])
    path, ticket = store.load(ws, t.id)
    doc = ticket_document(ws, ticket)
    jsonschema.validate(doc, ticket_schema())
    assert doc["needs"] == [{"kind": "answer", "detail": "Q1"}]
    assert doc["questions"][0]["hash"] == query.needs_you(ws)[0]["hashes"]["Q1"]


def test_round_trip_through_the_ticket_model(ws, aops):
    t = aops.new("Round trip", ask="ask text")
    _, ticket = store.load(ws, t.id)
    doc = ticket_document(ws, ticket)
    again = ticket_document(ws, ticket_from_document(json.loads(json.dumps(doc))))
    assert again == doc


def test_round_trip_keeps_an_invalidated_gate_invalidated(ws, working, aops):
    aops.set_section(working, "Requirements", "r changed")
    _, ticket = store.load(ws, working)
    doc = ticket_document(ws, ticket)
    assert doc["gates"]["requirements"]["state"] == "invalidated"
    again = ticket_document(ws, ticket_from_document(json.loads(json.dumps(doc))))
    assert again["gates"] == doc["gates"]


def test_cli_prints_schema_and_example_outside_a_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert run(["schema", "ticket", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == ticket_schema()
    assert run(["schema", "example"]) == 0
    assert json.loads(capsys.readouterr().out) == example_document()


def test_needs_you_carries_gate_hash(ws, put):
    from orch.core.gates import gate_hash
    tid = put("backlog", title="r", sections={"Requirements": "- a", "Acceptance criteria": "- [ ] b"})
    item = next(i for i in query.needs_you(ws) if i["kind"] == "approve-requirements")
    _, t = store.load(ws, tid)
    assert item["ticket"] == tid and item["gate"] == "requirements" and item["gate_hash"] == gate_hash(t, "requirements")


def test_needs_you_carries_the_plan_and_re_approve_gate_hash(ws, working, aops):
    from orch.core.gates import gate_hash
    aops.set_section(working, "Plan", "1. do it")
    item = next(i for i in query.needs_you(ws) if i["kind"] == "approve-plan")
    _, t = store.load(ws, working)
    assert item["gate"] == "plan" and item["gate_hash"] == gate_hash(t, "plan")
    aops.set_section(working, "Requirements", "r changed")
    item = next(i for i in query.needs_you(ws) if i["kind"] == "re-approve")
    _, t = store.load(ws, working)
    assert item["detail"] == "requirements" and item["gate"] == "requirements"
    assert item["gate_hash"] == gate_hash(t, "requirements")


def test_the_documented_verdict_hash_vector_matches():
    """Fix round 1: docs/ticket-schema.md carries a verdict-hash test vector for phone clients."""
    import re
    from pathlib import Path

    from orch.core.epics import verdict_hash
    from orch.core.model import new_ticket
    text = (Path(__file__).resolve().parents[1] / "docs" / "ticket-schema.md").read_text(encoding="utf-8")
    want = re.search(r"verdict_hash:\s+(sha256:[0-9a-f]{64})", text).group(1)
    t = new_ticket("DEMO-0038", "Export the meter readings as CSV", type="feature", priority="high", size="m",
                   created="2026-10-02T09:00Z")
    t.meta["status"] = "testing"
    t.set_section("Acceptance criteria", "- [ ] Opens in Excel")
    t.set_section("Verification", "- AC1: opened 3 files in Excel")
    assert verdict_hash([t], None) == want
    assert example_document()["verdict"] is None  # the example waits on a question: no verdict due


# 1.6 `signed`: the ledger's verdict on each approved gate and on a done ticket, never key material
def _signed(ws, tid):
    from orch.core import store
    return ticket_document(ws, store.load(ws, tid)[1])["signed"]


def test_signed_gate_and_verdict_name_a_human(ws, hops, working, put):
    assert _signed(ws, working) == {"requirements": {"signed": True, "by": "you"}}
    tid = put("testing", sections={"Verification": "ok"})
    hops.verdict(tid, "done")
    assert _signed(ws, tid)["verdict"] == {"signed": True, "by": "accepted"}
    _validate(ticket_document(ws, store.load(ws, tid)[1]))


def test_unsigned_approval_and_done_are_not_signed(ws, put):
    from orch.core.gates import gate_hash
    tid = put("open", sections={"Requirements": "r", "Acceptance criteria": "- [ ] a"})
    path, t = store.load(ws, tid)
    t.meta["gates"]["requirements"] = {"approved": "2026-10-01T09:00Z", "via": "tty", "hash": gate_hash(t, "requirements")}
    store.save(ws, t, path)
    assert _signed(ws, tid) == {"requirements": {"signed": False, "by": None}}
    assert _signed(ws, put("backlog")) == {}  # nothing approved, nothing to attest
    done = put("done", gates={"verify": {"verdict": "done", "at": "2026-10-05T10:00Z", "via": "tty"}})
    assert _signed(ws, done)["verdict"] == {"signed": False, "by": None}


def test_tampered_ledger_entry_is_not_signed(ws, working):
    from orch.core import ledger
    p = ledger.ledger_path(ws)
    # a field the lookup does not match on, so only the MAC can catch the edit
    p.write_text(p.read_text(encoding="utf-8").replace('"via": "tty"', '"via": "phone:x"'), encoding="utf-8")
    assert _signed(ws, working) == {"requirements": {"signed": False, "by": None}}


def test_missing_ledger_is_not_signed(ws, working):
    from orch.core import ledger
    ledger.ledger_path(ws).unlink()
    assert _signed(ws, working) == {"requirements": {"signed": False, "by": None}}


def test_phone_signed_gate(ws, aops, working):
    from orch.core.events import Actor
    from conftest import human_ops
    tid = aops.new("phone").id
    aops.set_section(tid, "Requirements", "r")
    aops.set_section(tid, "Acceptance criteria", "- [ ] a")
    human_ops(ws, Actor("human", "you", "phone:iPhone", device="dev1")).approve(tid, "requirements")
    assert _signed(ws, tid) == {"requirements": {"signed": True, "by": "from your phone"}}


def test_epic_charter_and_delegation(ws, aops, hops):
    from test_epics import _child, _epic
    eid = _epic(aops)
    hops.approve(eid, "requirements", delegate={"max_children": 2, "max_size": "m"})
    auto = _child(aops, eid, "auto")
    aops.epic_auto_approve(auto)
    assert _signed(ws, auto)["requirements"] == {"signed": False, "by": "by delegation"}


def test_charter_covered_child_is_signed_by_the_charter(ws, aops, hops):
    from test_epics import _child, _epic
    eid = _epic(aops)
    cid = _child(aops, eid, "one")
    hops.approve(eid, "requirements")
    assert _signed(ws, cid) == {"requirements": {"signed": True, "by": "by your epic charter"},
                                "plan": {"signed": True, "by": "by your epic charter"}}


def test_closed_by_a_human_is_signed_and_a_forged_close_is_not(ws, hops, put):
    tid = put("open")
    hops.close(tid, "duplicate")
    assert _signed(ws, tid)["verdict"] == {"signed": True, "by": "closed"}
    from orch.core import ledger
    p = ledger.ledger_path(ws)
    p.write_text("", encoding="utf-8")  # the signed close entry is gone, the human close event stays
    assert _signed(ws, tid)["verdict"] == {"signed": False, "by": None}


def test_verify_hash_is_in_schema_and_document(ws, put):
    done = put("done", gates={"verify": {"verdict": "done", "at": "2026-10-05T10:00Z", "via": "tty", "hash": "sha256:" + "a" * 64}})
    assert ticket_document(ws, store.load(ws, done)[1])["gates"]["verify"]["hash"] == "sha256:" + "a" * 64
    assert "hash" in ticket_schema()["properties"]["gates"]["properties"]["verify"]["properties"]
