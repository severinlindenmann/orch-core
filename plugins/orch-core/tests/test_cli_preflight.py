"""#7: human-only CLI commands check everything (gate name, -m, state, hash) before they ask for the typed
confirmation, and `--dry-run` says what would happen without asking or writing anything."""
import json

import pytest

from orch import actor
from orch.cli import run
from conftest import human_ops


@pytest.fixture
def human(monkeypatch, ws_root):
    """A human at a terminal whose `input` must not be called unless the test allows it."""
    monkeypatch.delenv("ORCH_HARNESS", raising=False)
    monkeypatch.setattr(actor, "is_interactive", lambda: True)
    prompts = []

    def refuse(prompt=""):
        prompts.append(prompt)
        raise AssertionError(f"asked for confirmation: {prompt}")

    monkeypatch.setattr("builtins.input", refuse)

    class Human:
        asked = prompts

        def types(self, text):
            def answer(prompt=""):
                prompts.append(prompt)
                return text
            monkeypatch.setattr("builtins.input", answer)
    return Human()


def _snapshot(ws):
    """Everything a write could touch: ticket files (with mtimes), the event log, gate snapshots. The scan's own
    read cache (.state/index.json, written by any read such as `orch show`) is not a write of the command."""
    out = {}
    for p in sorted(ws.home.rglob("*")):
        if p.is_file() and "locks" not in p.parts and p != ws.state_dir / "index.json":
            out[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
    return out


@pytest.mark.parametrize("args,code", [
    (["approve", "{tid}", "plan"], 3),                 # open: plans are approved in progress
    (["approve", "{tid}", "requirements"], 3),         # open: requirements are approved in backlog
    (["approve", "{tid}", "foo"], 2),                  # bad gate name
    (["move", "{tid}", "open"], 3),                    # already open
    (["verdict", "{tid}", "done"], 3),                 # open → done is not allowed
    (["verdict", "{tid}", "maybe"], 2),
    (["verdict", "{tid}", "follow-up"], 2),            # needs -m
    (["answer", "{tid}", "Q9", "yes"], 2),             # no such question
    (["request-changes", "{tid}", "plan"], 2),         # says nothing
])
def test_refusals_never_ask_for_confirmation(human, put, args, code, capsys):
    tid = put("open")
    assert run([a.format(tid=tid) for a in args]) == code
    assert human.asked == []
    assert "error" in capsys.readouterr().err


def test_valid_approve_asks_once_and_binds_the_previewed_hash(human, ws, put, capsys):
    from orch.core import store
    from orch.core.gates import gate_hash
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    seen = gate_hash(store.load(ws, tid)[1], "requirements")
    human.types(tid)
    assert run(["approve", tid, "requirements"]) == 0
    assert len(human.asked) == 1
    out = capsys.readouterr().out
    assert seen.removeprefix("sha256:")[:8] in out  # the hash it binds is shown before the prompt
    t = store.load(ws, tid)[1]
    assert t.status == "open" and t.meta["gates"]["requirements"]["hash"] == seen
    assert t.meta["gates"]["requirements"]["via"] == "tty"


def test_text_changed_between_preview_and_confirm_is_refused(human, ws, put, monkeypatch, capsys):
    from orch.core import store
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})

    def edit_then_confirm(prompt=""):
        _, t = store.load(ws, tid)
        t.set_section("Requirements", "changed while the human typed")
        store.save(ws, t)
        return tid

    monkeypatch.setattr("builtins.input", edit_then_confirm)
    assert run(["approve", tid, "requirements"]) != 0
    assert "changed since" in capsys.readouterr().err
    assert store.load(ws, tid)[1].status == "backlog"


@pytest.mark.parametrize("status,args_of", [
    ("backlog", lambda tid: ["approve", tid, "requirements"]),
    ("backlog", lambda tid: ["request-changes", tid, "requirements", "-m", "be specific"]),
    ("open", lambda tid: ["move", tid, "backlog"]),
])
def test_dry_run_writes_nothing_and_never_asks(human, ws, put, status, args_of, capsys):
    tid = put(status, sections={"Requirements": "r", "Acceptance criteria": "a"})
    before = _snapshot(ws)
    assert run([*args_of(tid), "--dry-run"]) == 0
    assert human.asked == []
    assert _snapshot(ws) == before  # no ticket write, no event, no gate snapshot, no ledger entry
    assert not (ws.state_dir / "gates").exists() or not any((ws.state_dir / "gates").iterdir())
    assert "would" in capsys.readouterr().out


def test_dry_run_reports_the_refusal(human, ws, put, capsys):
    tid = put("open")
    before = _snapshot(ws)
    assert run(["approve", tid, "plan", "--dry-run"]) == 3
    assert human.asked == [] and _snapshot(ws) == before


def test_dry_run_answer_and_verdict(human, ws, put, capsys):
    q = {"id": "Q1", "text": "Which env?", "type": "single",
         "options": [{"key": "A", "label": "dev"}, {"key": "B", "label": "prod"}], "recommended": "A",
         "blocking": True, "answer": None, "note": None, "answered": None, "via": None}
    asking = put("waiting", questions=[q])
    testing = put("testing", sections={"Verification": "ran it"})
    before = _snapshot(ws)
    assert run(["answer", asking, "Q1", "B", "--dry-run", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["dry_run"] is True and doc["status"] == "in-progress"
    assert run(["verdict", testing, "done", "--dry-run"]) == 0
    assert "would close" in capsys.readouterr().out
    assert _snapshot(ws) == before and human.asked == []


def test_dry_run_needs_no_terminal_but_stays_human_only(human, ws, put, monkeypatch, capsys):
    """--dry-run is read-only, so it needs no terminal; inside an agent harness it is refused like the real command."""
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    testing = put("testing", sections={"Verification": "ok"})
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    assert run(["approve", tid, "requirements", "--dry-run"]) == 0
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    before = _snapshot(ws)
    for args in (["approve", tid, "requirements"], ["request-changes", tid, "requirements", "-m", "x"],
                 ["verdict", testing, "done"], ["move", tid, "open"]):
        assert run([*args, "--dry-run"]) == 3, args
        err = capsys.readouterr().err
        assert "agent harness" in err or "human-only" in err, (args, err)
    assert _snapshot(ws) == before
    assert run(["approve", tid, "requirements"]) == 3  # a plain approve still refuses before anything else
    assert human.asked == []
    from orch.core.lifecycle import PREVIEW
    assert PREVIEW.get() is False  # the preview context never leaks


def test_verdict_is_bound_to_the_ticket_as_previewed(human, ws, put, monkeypatch, capsys):
    from orch.core import store
    tid = put("testing", sections={"Verification": "ran it"})

    def edit_then_confirm(prompt=""):
        _, t = store.load(ws, tid)
        t.set_section("Verification", "rewritten while the human typed")
        store.save(ws, t)
        return tid

    monkeypatch.setattr("builtins.input", edit_then_confirm)
    assert run(["verdict", tid, "done"]) != 0
    assert "changed since you reviewed it" in capsys.readouterr().err
    assert store.load(ws, tid)[1].status == "testing"


def test_move_is_bound_to_the_ticket_as_previewed(human, ws, put, monkeypatch, capsys):
    from orch.core import store
    tid = put("open")

    def edit_then_confirm(prompt=""):
        _, t = store.load(ws, tid)
        t.set_section("Context", "changed")
        store.save(ws, t)
        return tid

    monkeypatch.setattr("builtins.input", edit_then_confirm)
    assert run(["move", tid, "backlog"]) != 0
    assert store.load(ws, tid)[1].status == "open"


def test_dry_run_ops_never_writes_the_gate_snapshot(ws, put):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    tid = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "a"})
    before = _snapshot(ws)
    t = human_ops(ws, Actor("human", "you", "tty"), dry_run=True).approve(tid, "requirements")
    assert t.status == "open"  # what would happen
    assert _snapshot(ws) == before
