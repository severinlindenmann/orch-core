import json
import subprocess
import sys

import pytest

from orch import actor
from orch.cli import run


@pytest.fixture
def switch(monkeypatch, ws_root):
    class Switch:
        def agent(self):
            monkeypatch.setenv("ORCH_HARNESS", "test-agent")
            monkeypatch.setenv("ORCH_SESSION", "s-1")
            monkeypatch.setattr(actor, "is_interactive", lambda: False)

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)

    s = Switch()
    s.agent()
    return s


def test_full_lifecycle_via_cli(switch, tmp_path, capsys):
    def ok(*args):
        code = run(list(args))
        assert code == 0, (args, capsys.readouterr())

    tid = "L-0001"
    ok("new", "--title", "Backup nightly config")
    (tmp_path / "req.md").write_text("Nightly backup to git.", encoding="utf-8")
    (tmp_path / "ac.md").write_text("- [ ] runs nightly", encoding="utf-8")
    ok("section", "set", tid, "Requirements", "--file", str(tmp_path / "req.md"))
    ok("section", "set", tid, "Acceptance criteria", "--file", str(tmp_path / "ac.md"))
    (tmp_path / "q.yaml").write_text("questions:\n  - text: One repo?\n    type: confirm\n    recommended: yes\n", encoding="utf-8")
    ok("ask", tid, "--file", str(tmp_path / "q.yaml"))
    assert run(["approve", tid, "requirements"]) == 3  # agent refused

    switch.human(tid)
    ok("answer", tid, "Q1", "yes")
    ok("approve", tid, "requirements")

    switch.agent()
    ok("claim", tid)
    (tmp_path / "plan.md").write_text("1. add job", encoding="utf-8")
    ok("section", "set", tid, "Plan", "--file", str(tmp_path / "plan.md"))
    ok("section", "set", tid, "Verification", "-m", "job ran: exit 0")
    assert run(["move", tid, "testing"]) == 5  # plan gate missing

    switch.human(tid)
    ok("approve", tid, "plan")
    switch.agent()
    ok("task", "add", tid, "Add the nightly job")
    ok("task", "start", tid, "T1")
    ok("task", "done", tid, "T1", "-m", "job ran: exit 0")
    ok("move", tid, "testing")
    assert run(["verdict", tid, "done"]) == 3

    switch.human(tid)
    ok("verdict", tid, "done")
    capsys.readouterr()
    ok("show", tid, "--json")
    assert json.loads(capsys.readouterr().out)["status"] == "done"
    ok("check")


def test_move_as_human_needs_confirmation(switch, put):
    tid = put("in-progress")
    switch.human("WRONG")
    assert run(["move", tid, "backlog"]) == 3
    switch.human(tid)
    assert run(["move", tid, "backlog"]) == 0


def test_artifacts_and_maintenance(switch, put, tmp_path, capsys, ws):
    tid = put("open")
    f = tmp_path / "shot.png"
    f.write_bytes(b"png")
    assert run(["artifact", "add", tid, str(f)]) == 0
    capsys.readouterr()
    assert run(["artifact", "list", tid, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)  # the ticket's linked artifacts (frontmatter `artifacts`)
    assert [(r["name"], r["kind"]) for r in rows] == [("shot.png", "screenshot")]
    assert run(["index"]) == 0 and (ws.tickets_dir / "INDEX.md").exists()
    assert run(["tidy"]) == 0
    capsys.readouterr()
    assert run(["rules"]) == 0
    assert "agent may commit: no" in capsys.readouterr().out


def test_check_exit_code(switch, put, capsys):
    put("open")  # open without an approved requirements gate
    capsys.readouterr()
    assert run(["check", "--json"]) == 5
    assert any(f["code"] == "status-without-gate" for f in json.loads(capsys.readouterr().out))


def test_init(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--customer", "initech", "--prefix", "ddp"]) == 0
    home = tmp_path / "orchestrator"
    cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert cfg["customer"] == "initech" and cfg["id"]["prefix"] == "DDP"
    assert (home / "tickets" / "waiting").is_dir()
    assert "temporary/" in (home / ".gitignore").read_text(encoding="utf-8")
    assert run(["init", "--customer", "x"]) == 5  # already initialised
    assert run(["init", "--customer", "x", "--prefix", "1bad", "--dir", str(tmp_path / "other")]) == 5
    assert not (tmp_path / "other" / "orchestrator" / "config.json").exists()


def test_missing_required_option_exits_2(switch):
    assert run(["new"]) == 2


def test_cli_import_is_light():
    code = ("import sys, orch.cli; "
            "print(','.join(m for m in ('fastapi', 'uvicorn', 'markdown_it', 'jsonschema', 'orch.addons.loader') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == ""


def test_cli_approve_overrides_a_pending_change_request(switch, ws, put, hops):
    """Ruling (final M9): `orch approve` (human only) stays allowed while a change request is
    pending, as the human's override, and clears that gate's changes_pending."""
    from orch.core import store
    from orch.core.gates import changes_pending
    tid = put("backlog", sections={"Requirements": "- r", "Acceptance criteria": "- a"})
    switch.human(tid)  # the human's terminal: a tty human action is refused while an agent harness runs the process
    hops.request_changes(tid, "requirements", "split it")
    assert changes_pending(store.load(ws, tid)[1], "requirements")
    assert run(["approve", tid, "requirements"]) == 0
    t = store.load(ws, tid)[1]
    assert t.status == "open" and not changes_pending(t, "requirements")
    assert "changes_requested" not in t.meta["gates"]["requirements"]
