from pathlib import Path

import pytest

from orch.core import tasks as tk
from orch.core.constants import SECTIONS
from orch.core.model import new_ticket, render_ticket
from orch.errors import NotFoundError, ValidationError

FIXTURE = Path(__file__).parent / "fixtures" / "tasks-v1.md"
DOC = Path(__file__).parents[1] / "docs" / "tasks-format.md"


def _fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8").rstrip("\n")


def test_tasks_section_sits_between_plan_and_current_state():
    i = SECTIONS.index("Tasks")
    assert SECTIONS[i - 1] == "Plan" and SECTIONS[i + 1] == "Current state"
    t = new_ticket("L-0001", "x", type="feature", priority="normal", size="m", created="2026-10-02T08:00Z")
    for name in ("Current state", "Tasks", "Plan"):
        t.set_section(name, name.lower())
    assert "\n## Plan\n\nplan\n\n## Tasks\n\ntasks\n\n## Current state\n" in render_ticket(t)


def test_fixture_round_trips_byte_for_byte():
    assert tk.render(tk.parse(_fixture())) == _fixture()


def test_doc_embeds_the_fixture_verbatim():
    assert _fixture() in DOC.read_text(encoding="utf-8")
    assert tk.FORMAT == "orch.tasks.v1" and tk.FORMAT in DOC.read_text(encoding="utf-8")


def test_every_state_and_field_parses():
    items = tk.parse(_fixture())
    assert [t.state for t in items] == ["done", "done", "skipped", "done", "doing", "blocked", "todo", "todo"]
    t2, t5, t6, t7, t8 = (tk.find(items, i) for i in ("T2", "T5", "T6", "T7", "T8"))
    assert t2.refs[0] == tk.Ref("static", "DEMO-0043/serverless-notes.md", "findings per job")
    assert [str(r) for r in t5.refs] == ["file:dbt-models/resources/jobs/gold/", "ac:1"]
    assert t5.verify == "`databricks bundle deploy -t dev` and one green run per job"
    assert (t6.needs, t6.on, t6.why) == (["T5"], "DEMO-0042", "prod runs read finance.*; SELECT not granted yet")
    assert t7.added_after_approval and t7.needs == ["T5", "T6"]
    assert t8.owner == "human" and items[0].owner == "agent"


def test_parse_accepts_obsidian_slack_and_renders_canonical():
    text = "- [X] T2 second\r\n\r\n    - note: ok\r\n\t- Ref: AC:2\n- [ ] T10 tenth\n  - owner: agent"
    assert tk.render(tk.parse(text)) == "- [x] T2 second\n  - ref: ac:2\n  - note: ok\n- [ ] T10 tenth"
    assert tk.parse("") == [] and tk.render([]) == ""


@pytest.mark.parametrize("text,line,word", [
    ("- [ ] T1 a\nfree text", 2, "only task items"),
    ("- [ ] T1 a\n  - colour: red", 2, "unknown field"),
    ("- [ ] T1 a\n- [x] T1 b", 2, "twice"),
    ("- [?] T1 a", 1, "unknown box"),
    ("- [ ] T1", 1, "no text"),
    ("- [ ] T0 a", 1, "start at T1"),
    ("  - ref: file:x", 1, "only task items"),
    ("- [ ] T1 a\n  ```\n  code\n  ```", 2, "only task items"),
    ("- [ ] T1 a\n  - verify: x\n  - verify: y", 3, "twice"),
    ("- [ ] T1 a\n  - ref: file:../secret", 2, "without '..'"),
    ("- [ ] T1 a\n  - ref: static:/etc/passwd", 2, "without '..'"),
    ("- [ ] T1 a\n  - ref: url:javascript:alert(1)", 2, "http"),
    ("- [ ] T1 a\n  - ref: wiki:Page", 2, "must start with"),
    ("- [ ] T1 a\n  - on: soon", 2, "on:"),
    ("- [ ] T1 a\n  - owner: robot", 2, "owner"),
    ("- [ ] T1 a\n  - needs: 5", 2, "task id"),
    ("- [ ] T1 a\n  - added: yesterday", 2, "added:"),
    ("- [ ] T1 a\n  - note:", 2, "no value"),
])
def test_parse_errors_name_the_line(text, line, word):
    with pytest.raises(tk.TaskParseError) as e:
        tk.parse(text)
    assert e.value.line == line and word in e.value.message
    assert e.value.message.startswith(f"Tasks line {line}: ")


def test_refs_split_kind_target_and_label():
    r = tk.parse_ref("file:dbt-models/x.yml#L4-9 — the job")
    assert (r.kind, r.target, r.label) == ("file", "dbt-models/x.yml#L4-9", "the job")
    assert str(r) == "file:dbt-models/x.yml#L4-9 — the job"
    assert tk.parse_ref("q:q2").target == "Q2" and tk.parse_ref("ticket:demo-0042").target == "DEMO-0042"
    assert tk.parse_ref("url:https://x.test/a:b").target == "https://x.test/a:b"
    assert tk.parse_ref("ac:02").target == "2"
    assert tk.normalize_on("q3") == "Q3" and tk.normalize_on("t4") == "T4" and tk.normalize_on("abc-77") == "ABC-77"
    assert [tk.on_kind(x) for x in ("Q3", "T4", "ABC-77")] == ["question", "task", "key"]


def test_summary_next_open_and_human_ready():
    items = tk.parse(_fixture())
    assert tk.summary(items) == {"total": 8, "todo": 2, "doing": 1, "done": 3, "skipped": 1, "blocked": 1, "closed": 4}
    assert tk.next_task(items).id == "T5" and tk.doing(items).id == "T5"
    assert tk.open_ids(items) == ["T5", "T6", "T7", "T8"]
    assert tk.needs_open(tk.find(items, "T7"), items) == ["T5", "T6"]
    assert [t.id for t in tk.human_ready(items)] == ["T8"]
    assert tk.find(items, "t7").id == "T7"
    with pytest.raises(NotFoundError):
        tk.find(items, "T99")
    assert tk.next_number(items) == 9 and tk.next_number(items, {12}) == 13


def test_next_task_without_doing_skips_human_and_waiting_tasks():
    items = tk.parse("- [x] T1 a\n- [ ] T2 b\n  - owner: human\n- [ ] T3 c\n  - needs: T2\n- [ ] T4 d")
    assert tk.next_task(items).id == "T4"


def test_build_assigns_ids_and_validates():
    new = tk.build([{"text": " Write\n the job ", "refs": ["file:hub/jobs/x.yml", "ac:1"], "needs": ["t3"]},
                    "Plain text task", {"text": "Grant access", "owner": "human"}], start=4)
    assert [t.id for t in new] == ["T4", "T5", "T6"]
    assert new[0].text == "Write the job" and new[0].needs == ["T3"] and new[2].owner == "human"
    with pytest.raises(ValidationError, match="task 1: unknown keys"):
        tk.build([{"text": "x", "state": "done"}], 1)
    with pytest.raises(ValidationError, match="task 1: 'text' is required"):
        tk.build([{"refs": []}], 1)
    with pytest.raises(ValidationError, match="task 2: ref"):
        tk.build(["ok", {"text": "x", "refs": ["file:../x"]}], 1)


def test_tasks_file_yaml():
    raw = tk.parse_tasks_file("tasks:\n  - text: A\n    refs: [file:hub/a.py, ac:1, url:https://x.test/p]\n    needs: [T1]\n")
    assert raw == [{"text": "A", "refs": ["file:hub/a.py", "ac:1", "url:https://x.test/p"], "needs": ["T1"]}]
    with pytest.raises(ValidationError):
        tk.parse_tasks_file("nope: 1")


def test_needs_problems():
    items = tk.parse("- [ ] T1 a\n  - needs: T2\n- [ ] T2 b\n  - needs: T1\n- [ ] T3 c\n  - needs: T9")
    unknown, cycle = tk.needs_problems(items)
    assert unknown == [("T3", "T9")] and cycle == ["T1", "T2", "T1"]
    assert tk.needs_problems(tk.parse("- [ ] T1 a\n- [ ] T2 b\n  - needs: T1")) == ([], None)


def test_newly_done_agent_tasks():
    old = tk.parse("- [/] T1 a\n- [ ] T2 b\n  - owner: human")
    new = tk.parse("- [x] T1 a\n- [x] T2 b\n  - owner: human\n- [x] T3 c")
    assert tk.newly_done_agent_tasks(old, new) == ["T1", "T3"]


@pytest.mark.parametrize("ref", ["file:hub/jobs/*.yml", "static:notes/a?.md", "artifact:run-*.csv"])
def test_glob_refs_are_a_parse_error(ref):
    with pytest.raises(ValueError, match="glob"):
        tk.parse_ref(ref)
    with pytest.raises(tk.TaskParseError, match="Tasks line 2: .*glob"):
        tk.parse(f"- [ ] T1 a\n  - ref: {ref}")
