"""#216: `orch new --body-file` splits every writable section and a `## Tasks` block; `orch section set <id>
--body-file` writes several sections in one locked write. Gates are never approved by either."""
import json

import pytest

from orch.cli import run
from orch.core import store, tasks as tk
from orch.core.body import BODY_SECTIONS, split_body
from orch.errors import UsageError, ValidationError

SPEC = """The customer wants exports to be faster.

## Summary
- speed up the export

## Requirements
- under 2 s

## Acceptance criteria
- [ ] export of 10k rows takes under 2 s
- [ ] no regression in the csv format

## Out of scope
- the pdf export

## Context
Seen in the March incident.

## Plan
1. profile
2. fix

## Verification
Nothing yet.

## Tasks
```yaml
tasks:
  - text: Profile the export
    verify: uv run pytest -q
  - text: Fix the hot loop
    needs: [T1]
```
"""


@pytest.fixture
def agent_cli(monkeypatch, ws_root):
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.setenv("ORCH_SESSION", "s-1")
    return ws_root


def _t(ws, tid):
    return store.load(ws, tid)[1]


# -- split ---------------------------------------------------------------------------------------------------------

def test_split_with_every_writable_section():
    ask, parts = split_body(SPEC, names=BODY_SECTIONS)
    assert ask.startswith("The customer wants")
    assert set(parts) == {"Summary", "Requirements", "Acceptance criteria", "Out of scope", "Context", "Plan",
                          "Verification", "Tasks"}
    assert "profile" in parts["Plan"] and "Profile the export" in parts["Tasks"]


@pytest.mark.parametrize("name", ["Log", "Ask"])
def test_log_and_ask_headings_are_still_refused(name):
    with pytest.raises(UsageError, match=name):
        split_body(f"words\n\n## {name}\nx\n", names=BODY_SECTIONS)


def test_the_default_split_is_unchanged_for_the_dashboard():
    with pytest.raises(UsageError, match="Plan"):
        split_body("words\n\n## Plan\nx\n")


# -- orch new ------------------------------------------------------------------------------------------------------

def test_new_writes_sections_and_tasks(ws, aops, ):
    ask, parts = split_body(SPEC, names=BODY_SECTIONS)
    t = aops.new("Faster export", ask=ask, sections=parts)
    on_disk = _t(ws, t.id)
    assert on_disk.section("Plan").startswith("1. profile")
    assert on_disk.section("Context").startswith("Seen in")
    items = tk.ticket_tasks(on_disk)
    assert [(i.id, i.text, i.needs, i.state) for i in items] == [
        ("T1", "Profile the export", [], "todo"), ("T2", "Fix the hot loop", ["T1"], "todo")]
    assert items[0].verify == "uv run pytest -q"
    assert all(not (g or {}).get("approved") for g in (on_disk.meta.get("gates") or {}).values())


def test_new_emits_one_task_added_event_per_task(ws, aops):
    from orch.core.events import read_events
    from orch.core.ops_tasks import used_numbers
    ask, parts = split_body(SPEC, names=BODY_SECTIONS)
    t = aops.new("Faster export", ask=ask, sections=parts)
    evs = read_events(ws, t.id)
    added = [e for e in evs if e.kind == "task.added"]
    assert [e.data for e in added] == [{"tasks": ["T1"], "after_approval": False},
                                       {"tasks": ["T2"], "after_approval": False}]
    assert evs[0].kind == "ticket.created" and evs[0].data["tasks"] == ["T1", "T2"]
    assert used_numbers(ws, t.id) == {1, 2}
    plain = aops.new("no tasks")
    assert not [e for e in read_events(ws, plain.id) if e.kind == "task.added"]


def test_new_refuses_bad_tasks_before_allocating_an_id(ws, aops):
    bad = {"Tasks": "tasks:\n  - text: a\n    needs: [T9]\n"}
    with pytest.raises(ValidationError):
        aops.new("x", sections=bad)
    with pytest.raises(ValidationError):
        aops.new("x", sections={"Tasks": "tasks:\n  - bogus: 1\n"})
    with pytest.raises(ValidationError):
        aops.new("x", sections={"Tasks": "not: [valid"})
    assert store.scan(ws) == [] or len(store.scan(ws)) == 0
    assert aops.new("ok").id == "L-0001"


def test_new_refuses_ticked_criteria_without_evidence(ws, aops):
    with pytest.raises(ValidationError, match="tick"):
        aops.new("x", sections={"Acceptance criteria": "- [x] done already", "Plan": "1. x"})
    t = aops.new("y", sections={"Acceptance criteria": "- [x] done already",
                                "Verification": "- AC1: ran it (`pytest`)"})
    assert t.id


def test_new_plan_on_an_epic_is_refused(ws, aops):
    with pytest.raises(UsageError, match="epic"):
        aops.new("E", type="epic", sections={"Plan": "1. x"})


def test_new_cli_body_file(agent_cli, ws, tmp_path, capsys):
    f = tmp_path / "spec.md"
    f.write_text(SPEC, encoding="utf-8")
    assert run(["new", "--title", "Faster export", "--body-file", str(f), "--json"]) == 0
    tid = json.loads(capsys.readouterr().out)["id"]
    t = _t(ws, tid)
    assert t.section("Plan").startswith("1. profile") and len(tk.ticket_tasks(t)) == 2
    assert t.section("Ask").startswith("The customer wants")
    assert run(["new", "--title", "Again", "--body-file", str(f), "--plan-file", str(f)]) != 0  # no such option


# -- section set ---------------------------------------------------------------------------------------------------

def test_section_set_body_file_writes_several_sections_at_once(ws, aops, tmp_path):
    t = aops.new("x")
    parts = {"Requirements": "- r", "Acceptance criteria": "- [ ] a", "Plan": "1. do"}
    out = aops.set_sections(t.id, parts)
    on_disk = _t(ws, t.id)
    assert on_disk.section("Plan") == "1. do" and on_disk.section("Requirements") == "- r"
    log = [l for l in on_disk.section("Log").splitlines() if "updated" in l]
    assert len(log) == 1 and "Plan" in log[0] and "Requirements" in log[0]
    from orch.core.events import read_events
    ev = read_events(ws, t.id)[-1]
    assert ev.kind == "ticket.edited" and ev.data["sections"] == list(parts)
    assert out.id == t.id


def test_section_set_is_all_or_nothing(ws, aops):
    t = aops.new("x")
    with pytest.raises(ValidationError, match="tick"):
        aops.set_sections(t.id, {"Plan": "1. do", "Acceptance criteria": "- [x] sneaky"})
    assert _t(ws, t.id).section("Plan") == ""
    with pytest.raises(UsageError, match="Log"):
        aops.set_sections(t.id, {"Plan": "1. do", "Log": "forged"})
    assert _t(ws, t.id).section("Plan") == ""


def test_section_set_tick_with_evidence_in_the_same_file_is_fine(ws, aops):
    t = aops.new("x")
    aops.set_sections(t.id, {"Acceptance criteria": "- [x] a", "Verification": "- AC1: ran it (`pytest`)"})
    assert _t(ws, t.id).section("Acceptance criteria") == "- [x] a"


def test_section_set_cli(agent_cli, ws, aops, tmp_path, capsys):
    t = aops.new("x")
    f = tmp_path / "s.md"
    f.write_text("## Requirements\n- r\n\n## Plan\n1. do\n", encoding="utf-8")
    assert run(["section", "set", t.id, "--body-file", str(f)]) == 0
    assert _t(ws, t.id).section("Plan") == "1. do"
    # text before the first heading would be the Ask, a Tasks block belongs to `orch task`, a name conflicts
    f.write_text("loose words\n\n## Plan\n1. x\n", encoding="utf-8")
    assert run(["section", "set", t.id, "--body-file", str(f)]) != 0
    f.write_text("## Tasks\ntasks:\n  - text: a\n", encoding="utf-8")
    assert run(["section", "set", t.id, "--body-file", str(f)]) != 0
    assert run(["section", "set", t.id, "Plan", "-m", "x", "--body-file", str(f)]) != 0
    assert run(["section", "set", t.id, "Plan", "-m", "single", "--json"]) == 0
