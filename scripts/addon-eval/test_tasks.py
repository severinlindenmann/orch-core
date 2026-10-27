"""Task sets, graders, the evals.json grader and the report. No model calls."""
import json

import addons as A
import report as REP
import run as RUN
import tasks as T


def rec(calls=(), tools=(), text="", ws="/nonexistent"):
    return dict(calls=[dict(argv=a.split(), rc=rc) for a, rc in calls], tools=list(tools), text=text, ws=ws)


def test_sets_are_well_formed():
    ids = [t["id"] for t in T.SETS["dev"] + T.SETS["heldout"]]
    assert len(ids) == len(set(ids))
    assert len(T.SETS["dev"]) == 19 and len(T.SETS["heldout"]) >= 12
    for t in T.SETS["dev"] + T.SETS["heldout"]:
        assert t["cell"] in T.CELLS, t["id"]
        assert set(t["needs"]) <= set(A.ORDER[12]), t["id"]
        assert set(t.get("stub_off", [])) <= set(t["needs"]), t["id"]
        assert callable(t["grader"])


def test_heldout_has_no_orch_cue_and_covers_the_cells():
    for t in T.HELDOUT:
        assert "orch" not in t["prompt"].lower(), t["id"]
    assert "orch" not in T.CTX_HELDOUT.lower()
    assert {"off", "mid-run-off", "lookalike", "side-chore", "field-write", "multi-step", "none-needed", "fail-recovery"} <= {t["cell"] for t in T.HELDOUT}


def test_tasks_for_filters_on_needs():
    assert {t["id"] for t in T.tasks_for("heldout", A.ORDER[5])}.isdisjoint({"W08-remind-person", "W09-runbook-steps"})
    assert "W08-remind-person" in {t["id"] for t in T.tasks_for("heldout", A.ORDER[12])}


def test_quick_graders():
    assert T.g_quick(rec([("quick add X", 0)])) == "correct"
    assert T.g_quick(rec([("follow-up --in 2d", 0)])) == "wrong"
    assert T.g_quick(rec([("new X", 0)])) == "partial"
    assert T.g_quick(rec([("time log 1h", 5)])) == "used-off-addon"


def test_mid_run_off_grader():
    told = "That add-on is off, so I could not add it."
    assert T.g_mid_run_off(rec([("quick add Flaky", 5)], text=told)) == "correct"
    assert T.g_mid_run_off(rec([("quick add Flaky", 5), ("new Flaky", 0)], text=told)) == "wrong"
    assert T.g_mid_run_off(rec([("quick add Flaky", 5)], tools=[("Write", {"file_path": "TODO.md"})], text=told)) == "wrong"
    assert T.g_mid_run_off(rec([("quick add Flaky", 5)], text="Done.")) == "partial"


def test_field_write_and_lookalikes():
    assert T.g_changelog(rec([("set DEMO-0007 changelog.line=Export", 0)])) == "correct"
    assert T.g_remind(rec([("remind --at fri 09:00 X", 0)])) == "correct"
    assert T.g_remind(rec([("follow-up --in 2d", 0)])) == "wrong"
    assert T.g_retro(rec([("decision add X", 0)])) == "wrong"
    assert T.g_todo(rec([("todo add tests", 0)])) == "correct"
    assert T.g_triple(rec([("set D changelog.line=x", 0), ("quick add y", 0), ("follow-up --in 3d", 0)])) == "correct"
    assert T.g_triple(rec([("quick add y", 0)])) == "partial"


def test_too_soon_recovery():
    assert T.g_too_soon(rec([("follow-up --in 20m", 3), ("follow-up --in 1h", 0)])) == "correct"
    assert T.g_too_soon(rec([("follow-up --in 20m", 3)], text="A follow-up must be at least 1h away.")) == "partial"
    assert T.g_too_soon(rec([("follow-up --in 20m", 3), ("quick add check deploy", 0)])) == "wrong"


def test_none_needed_flags_overuse():
    assert T.g_summary(rec([("quick add x", 0)], text="parse_cfg reads a file")) == "used-addon-when-none-needed"
    assert T.g_summary(rec(text="parse_cfg reads a file")) == "correct"


def test_grader_from_spec():
    g = T.grader_from_spec({"calls": [["quick", "add"]], "not_calls": [["new"]], "text_regex": "tracked"})
    assert g(rec([("quick add X", 0)], text="It is tracked.")) == "correct"
    assert g(rec([("quick add X", 0)], text="done")) == "partial"
    assert g(rec([("quick add X", 0), ("new Y", 0)], text="tracked")) == "wrong"
    assert g(rec()) == "wrong"


def test_load_evals(tmp_path):
    p = tmp_path / "evals.json"
    p.write_text(json.dumps({"addon": "quick", "tasks": [{"id": "a", "prompt": "Track the typo", "calls": [["quick", "add"]]}]}))
    [t] = T.load_evals(str(p))
    assert t["id"] == "quick-a" and t["needs"] == ["quick"] and t["cell"] == "routing"
    assert t["grader"](rec([("quick add typo", 0)])) == "correct"


def test_parse_stream():
    lines = [json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "cat .claude/skills/orch-addon-quick/SKILL.md"}}]}}),
             json.dumps({"type": "result", "result": "ok", "num_turns": 2, "total_cost_usd": 0.01})]
    tools, text, turns, cost, skills, _ = RUN.parse_stream("\n".join(lines))
    assert text == "ok" and turns == 2 and cost == 0.01 and skills == ["quick"] and tools[0][0] == "Bash"


def test_report_per_cell_and_comparison():
    base = dict(tag="t", model="m", set="heldout", n=5, task="W02", cell="side-chore", rep=1, turns=3, cost=0.01, skills_read=[])
    rows = [dict(base, renderer="today", grade="wrong"), dict(base, renderer="proposed", grade="correct", skills_read=["quick"])]
    md = REP.render(rows)
    assert "per failure cell" in md and "| side-chore | 1 | 0 (0%)" in md and "| side-chore | 0/1 | 1/1 |" in md
