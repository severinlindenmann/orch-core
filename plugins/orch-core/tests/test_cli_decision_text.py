"""Final review I1: every human-only decision in the terminal prints exactly what it binds before the typed id:
the gated text for `orch approve`, criteria and evidence for `orch verdict`, the question and the answer for
`orch answer`, and binds it (a change while the human reads is refused)."""
import pytest

from orch import actor
from orch.cli import run
from orch.core import store


@pytest.fixture
def switch(monkeypatch, ws_root):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)

    s = Switch()
    s.agent()
    return s


def _ok(capsys, *args):
    code = run(list(args))
    out = capsys.readouterr()
    assert code == 0, (args, out)
    return out.out


def _ws(ws_root):
    from orch.core.workspace import Workspace
    return Workspace.open(ws_root)


def _agent_ops(ws_root):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    return Ops(_ws(ws_root), Actor("agent", "x", "cli", "s-1"))


def _confirm(monkeypatch, capsys, shown, typed, during=None):
    def confirm(prompt=""):
        shown["before"] = capsys.readouterr().out
        if during:
            during()
        return typed
    monkeypatch.setattr("builtins.input", confirm)


def _ticket(capsys):
    _ok(capsys, "new", "--title", "Export")
    _ok(capsys, "section", "set", "L-0001", "Summary", "-m", "- invoices leave as CSV")
    _ok(capsys, "section", "set", "L-0001", "Requirements", "-m", "export every invoice")
    _ok(capsys, "section", "set", "L-0001", "Acceptance criteria", "-m", "- [ ] opens in Excel")
    _ok(capsys, "section", "set", "L-0001", "Out of scope", "-m", "no PDF")


def test_approve_prints_the_whole_gated_text_before_the_typed_id(switch, ws_root, capsys, monkeypatch):
    _ticket(capsys)
    switch.human()
    shown = {}
    _confirm(monkeypatch, capsys, shown, "L-0001")
    assert run(["approve", "L-0001", "requirements"]) == 0
    for text in ("## Summary", "invoices leave as CSV", "## Requirements", "export every invoice",
                 "opens in Excel", "## Out of scope", "no PDF", "size: m", "type: feature"):
        assert text in shown["before"], text


def test_approve_refuses_text_changed_while_the_human_reads(switch, ws_root, capsys, monkeypatch):
    _ticket(capsys)
    switch.human()
    shown = {}
    _confirm(monkeypatch, capsys, shown, "L-0001",
             during=lambda: _agent_ops(ws_root).set_section("L-0001", "Out of scope", "PDF too"))
    assert run(["approve", "L-0001", "requirements"]) != 0
    assert store.resolve(_ws(ws_root), "L-0001").status == "backlog"


def _in_testing(switch, capsys, monkeypatch):
    _ticket(capsys)
    switch.human()
    monkeypatch.setattr("builtins.input", lambda prompt="": "L-0001")
    _ok(capsys, "approve", "L-0001", "requirements")
    switch.agent()
    _ok(capsys, "claim", "L-0001")
    _ok(capsys, "section", "set", "L-0001", "Plan", "-m", "1. write it")
    switch.human()
    _ok(capsys, "approve", "L-0001", "plan")
    switch.agent()
    _ok(capsys, "task", "add", "L-0001", "the work")
    _ok(capsys, "task", "start", "L-0001", "T1")
    _ok(capsys, "task", "done", "L-0001", "T1", "-m", "ok")
    _ok(capsys, "section", "set", "L-0001", "Verification", "-m", "- AC1: exported 3 invoices")
    _ok(capsys, "move", "L-0001", "testing")
    switch.human()


def test_verdict_prints_criteria_and_evidence_and_binds_them(switch, ws_root, capsys, monkeypatch):
    _in_testing(switch, capsys, monkeypatch)
    shown = {}
    _confirm(monkeypatch, capsys, shown, "L-0001",
             during=lambda: _agent_ops(ws_root).set_section("L-0001", "Verification", "- AC1: something else"))
    assert run(["verdict", "L-0001", "done"]) != 0
    assert "opens in Excel" in shown["before"] and "exported 3 invoices" in shown["before"]
    assert store.resolve(_ws(ws_root), "L-0001").status == "testing"
    monkeypatch.setattr("builtins.input", lambda prompt="": "L-0001")
    _ok(capsys, "verdict", "L-0001", "done")
    assert store.resolve(_ws(ws_root), "L-0001").status == "done"


def test_answer_prints_the_question_options_and_the_chosen_answer(switch, ws_root, capsys, monkeypatch, tmp_path):
    _ticket(capsys)
    qf = tmp_path / "q.yaml"
    qf.write_text("questions:\n  - text: Which region?\n    why: data residency\n    options:\n"
                  "      - {key: A, label: Frankfurt}\n      - {key: B, label: Zurich, cost: more}\n",
                  encoding="utf-8")
    _ok(capsys, "ask", "L-0001", "--file", str(qf))
    switch.human()
    shown = {}
    _confirm(monkeypatch, capsys, shown, "L-0001")
    assert run(["answer", "L-0001", "Q1", "B"]) == 0
    before = shown["before"]
    for text in ("Which region?", "data residency", "A", "Frankfurt", "Zurich", "more"):
        assert text in before, text
    assert "answer: B" in before


def test_request_changes_prints_the_gated_text_before_the_typed_id(switch, ws_root, capsys, monkeypatch):
    _ticket(capsys)
    switch.human()
    shown = {}
    _confirm(monkeypatch, capsys, shown, "L-0001")
    assert run(["request-changes", "L-0001", "requirements", "-m", "narrower please"]) == 0
    for text in ("Requesting changes on the requirements", "export every invoice", "no PDF", "size: m"):
        assert text in shown["before"], text


def test_cli_done_verdict_on_a_changed_approval_is_refused(switch, ws_root, capsys, monkeypatch):
    _in_testing(switch, capsys, monkeypatch)
    _agent_ops(ws_root).set_section("L-0001", "Out of scope", "PDF after all")
    monkeypatch.setattr("builtins.input", lambda prompt="": "L-0001")
    assert run(["verdict", "L-0001", "done"]) != 0
    assert "changed since approval" in capsys.readouterr().err
    assert store.resolve(_ws(ws_root), "L-0001").status == "testing"
