"""docs/tasks-view.schema.json is the JSON contract of tasks_view.view() (format orch.tasks.v1)."""
import json
from pathlib import Path

import jsonschema
import pytest

from orch.core import store, tasks_view

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "docs" / "tasks-view.schema.json").read_text(encoding="utf-8"))
FIXTURE = (Path(__file__).parent / "fixtures" / "tasks-v1.md").read_text(encoding="utf-8")


def _validate(v):
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    jsonschema.Draft202012Validator(SCHEMA).validate(json.loads(json.dumps(v, default=str)))


def test_schema_is_versioned():
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert SCHEMA["properties"]["format"] == {"const": "orch.tasks.v1"}


def test_golden_fixture_view_matches_the_schema(ws, put):
    tid = put("in-progress", sections={"Tasks": FIXTURE})
    v = tasks_view.view(ws, store.load(ws, tid)[1])
    assert v["error"] is None and len(v["tasks"]) == 8
    _validate(v)


def test_every_ref_and_on_kind_matches_the_schema(ws_root, configure, aops, hops, working):
    ws2 = configure(git={"repos": {"hub": {}}},
                    external_trackers=[{"prefix": "ABC", "pattern": "ABC-\\d+", "url": "https://jira.test/browse/{key}"}])
    other = aops.new("Access").id
    aops.ask(working, [{"text": "Which?", "options": ["a", "b"], "blocking": False}])
    aops.task_add(working, [
        {"text": "all refs", "refs": ["file:hub/x.yml#L2-4", "static:n.md", "artifact:a.csv", f"ticket:{other}",
                                      "ticket:ZZZ-9", "ext:ABC-7", "url:https://x.test — docs", "section:Decisions",
                                      "ac:1", "q:Q1"]},
        {"text": "q"}, {"text": "t"}, {"text": "k"}, {"text": "e"}, {"text": "human", "owner": "human"}])
    aops.task_block(working, "T2", "w", on="Q1")
    aops.task_block(working, "T3", "w", on="T6")
    aops.task_block(working, "T4", "w", on=other)
    aops.task_block(working, "T5", "w", on="ABC-8")
    v = tasks_view.view(ws2, store.load(ws2, working)[1])
    assert {t["on_ref"]["kind"] for t in v["tasks"] if t["on_ref"]} == {"question", "task", "ticket", "ext"}
    _validate(v)


def test_broken_view_matches_the_schema(ws, put):
    tid = put("in-progress", sections={"Tasks": "- [ ] T1 a\nfree text"})
    v = tasks_view.view(ws, store.load(ws, tid)[1])
    assert v["line"] == 2
    _validate(v)


def test_schema_rejects_an_unknown_key(ws, put):
    tid = put("in-progress", sections={"Tasks": FIXTURE})
    v = tasks_view.view(ws, store.load(ws, tid)[1])
    v["tasks"][0]["refs"][0]["surprise"] = 1
    with pytest.raises(jsonschema.ValidationError):
        _validate(v)
