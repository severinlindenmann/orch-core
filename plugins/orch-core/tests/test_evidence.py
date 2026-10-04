"""Verification tied to acceptance criteria (ticket design review §1.3, §3.1): Verification lines cite AC1…ACn,
a criterion counts as proven only with evidence, ticking one needs evidence, and handing over warns."""
import pytest

from orch.core import evidence, store
from orch.core.model import new_ticket
from orch.errors import ValidationError

AC = "- [ ] downloads retry with backoff\n- [ ] a 404 is not retried\n- [x] the log names the gateway"


def _t(verification="", ac=AC):
    t = new_ticket("L-1", "t", type="feature", priority="normal", size="m", created="2026-10-01T09:00Z")
    t.set_section("Acceptance criteria", ac)
    t.set_section("Verification", verification)
    return t


def test_criteria_are_numbered_in_order_with_their_tick():
    cs = evidence.criteria(_t())
    assert [(c.n, c.text, c.ticked) for c in cs] == [
        (1, "downloads retry with backoff", False), (2, "a 404 is not retried", False),
        (3, "the log names the gateway", True)]
    assert all(c.evidence == () for c in cs)


@pytest.mark.parametrize("line, cited", [
    ("- AC1: pytest tests/test_retry.py 12 passed", [1]),
    ("- AC1, AC3: run 9001 log shows gateway g-7", [1, 3]),
    ("- ac2 — curl returned 404 once, no retry", [2]),
    ("* pytest -k backoff passed (AC1)", [1]),
    ("- AC9: no such criterion", []),
    ("- pytest passed", []),
    ("  - AC1: an indented line belongs to the line above", []),
])
def test_verification_lines_cite_criteria(line, cited):
    cs = evidence.criteria(_t(line))
    assert [c.n for c in cs if c.evidence] == cited


def test_the_ac_prefix_is_not_repeated_in_the_evidence():
    c = evidence.criteria(_t("- AC1: pytest 12 passed\n  ```\n  12 passed\n  ```"))[0]
    assert c.evidence[0].startswith("pytest 12 passed")
    assert "12 passed" in c.evidence[0].split("\n", 1)[1]  # continuation lines stay with their line


def test_progress_counts_only_criteria_with_evidence():
    assert evidence.progress(_t("- AC2: curl -i returned 404\n- something else")) == (1, 3)
    assert evidence.progress(_t(ac="")) == (0, 0)


def test_lines_citing_no_criterion_are_other_evidence():
    assert evidence.other_evidence(_t("- AC1: a\n- ruff check passed\nfree text")) == ["ruff check passed", "free text"]


def test_missing_lists_unproven_criteria():
    assert evidence.missing(_t("- AC2: curl returned 404 once")) == [1, 3]


# -- ticking needs evidence ---------------------------------------------------------------------------------------

def test_ticking_a_criterion_without_evidence_is_refused(aops, working):
    with pytest.raises(ValidationError) as e:
        aops.set_section(working, "Acceptance criteria", "- [x] every job on serverless\n- [ ] cost compared")
    assert "AC1" in e.value.message and "Verification" in (e.value.hint or "")


def test_ticking_a_criterion_with_evidence_works(aops, working):
    aops.set_section(working, "Verification", "- AC1: databricks jobs list shows 14 serverless jobs")
    t = aops.set_section(working, "Acceptance criteria", "- [x] every job on serverless\n- [ ] cost compared")
    assert evidence.criteria(t)[0].ticked


def test_a_tick_that_was_already_there_is_not_refused(ws, aops, put):
    tid = put("in-progress", sections={"Acceptance criteria": "- [x] old tick\n- [ ] b"})
    t = aops.set_section(tid, "Acceptance criteria", "- [x] old tick\n- [ ] b, reworded")
    assert t.section("Acceptance criteria").startswith("- [x] old tick")


# -- handing over warns -------------------------------------------------------------------------------------------

def _ready(aops, hops, working, close_tasks, verification):
    aops.set_section(working, "Plan", "1. migrate")
    hops.approve(working, "plan")
    close_tasks(aops, working)
    aops.set_section(working, "Verification", verification)


def test_move_to_testing_warns_about_criteria_without_evidence(aops, hops, working, close_tasks):
    _ready(aops, hops, working, close_tasks, "- AC1: 14 jobs on serverless")
    t = aops.move(working, "testing")
    assert t.status == "testing"
    assert any("AC2" in w for w in aops.warnings)


def test_move_to_testing_with_every_criterion_proven_does_not_warn_about_them(aops, hops, working, close_tasks):
    _ready(aops, hops, working, close_tasks, "- AC1: 14 jobs moved to serverless\n- AC2: cost sheet attached")
    aops.move(working, "testing")
    assert not any("AC" in w for w in aops.warnings)


def test_cli_prints_move_warnings(ws, aops, hops, working, close_tasks, capsys, monkeypatch):
    from orch.cli import run
    _ready(aops, hops, working, close_tasks, "- AC1: 14 jobs")
    monkeypatch.setenv("ORCH_HARNESS", "claude-code")
    monkeypatch.setenv("ORCH_SESSION", aops.actor.session)
    assert run(["move", working, "testing"]) == 0
    assert "warning:" in capsys.readouterr().err


def test_check_reports_ticks_without_evidence(ws, put):
    from orch.core.check import run_checks
    tid = put("testing", sections={"Acceptance criteria": "- [x] a\n- [ ] b", "Verification": "- AC2: pytest 3 passed"})
    found = [f for f in run_checks(ws, emit_events=False) if f.code == "criterion-ticked-without-evidence"]
    assert [(f.ticket, f.level) for f in found] == [(tid, "warning")] and "AC1" in found[0].message


# -- review fix round 1: placeholders prove nothing, and the guard refuses unproven ticks -------------------------

@pytest.mark.parametrize("line, counts", [
    ("- AC1: todo", False), ("- AC1: TBD later", False), ("- AC1: n/a", False), ("- AC1: ?", False),
    ("- AC1: ok", False), ("- AC1: …", False), ("- AC1:", False), ("- AC1: (todo) run it", False),
    ("- AC1: pytest 12 passed", True), ("- AC1: see artifact report.html", True),
    ("- AC1: ok\n  ```\n  12 passed in 0.4s\n  ```", True),  # the output under the line is part of the evidence
])
def test_placeholder_lines_are_not_evidence(line, counts):
    assert bool(evidence.criteria(_t(line))[0].evidence) is counts


def test_guard_refuses_an_agent_tick_without_evidence(ws, put):
    from orch.hooks.guard import evaluate
    tid = put("in-progress", sections={"Acceptance criteria": "- [ ] a\n- [ ] b", "Verification": "- AC2: pytest 3 passed"})
    path = str(store.resolve(ws, tid).path)
    edit = lambda old, new: {"tool_name": "Edit", "tool_input": {"file_path": path, "old_string": old, "new_string": new}}
    d = evaluate(ws, edit("- [ ] a", "- [x] a"))
    assert not d.allow and "AC1" in d.reason
    assert evaluate(ws, edit("- [ ] b", "- [x] b")).allow  # AC2 has evidence
    assert evaluate(ws, edit("pytest 3 passed", "pytest 4 passed")).allow  # editing Verification is fine


def test_section_set_refuses_a_tick_backed_only_by_a_placeholder(aops, working):
    aops.set_section(working, "Verification", "- AC1: TODO")
    with pytest.raises(ValidationError):
        aops.set_section(working, "Acceptance criteria", "- [x] every job on serverless\n- [ ] cost compared")
