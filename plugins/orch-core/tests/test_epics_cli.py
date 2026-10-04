"""E2 CLI: `orch new --type epic|--epic|--sprint`, `orch link --epic|--sprint`, `orch approve <epic> --delegate`,
`orch epic show|auto-approve|pause`, `orch sprint list|current`."""
import json

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

        def human(self, confirm):
            monkeypatch.delenv("ORCH_HARNESS", raising=False)
            monkeypatch.setattr(actor, "is_interactive", lambda: True)
            monkeypatch.setattr("builtins.input", lambda prompt="": confirm)

    s = Switch()
    s.agent()
    return s


def _ok(capsys, *args):
    code = run(list(args))
    out = capsys.readouterr()
    assert code == 0, (args, out)
    return out.out


def _refine(capsys, tid, plan=True):
    _ok(capsys, "section", "set", tid, "Requirements", "-m", "r")
    _ok(capsys, "section", "set", tid, "Acceptance criteria", "-m", "- [ ] a")
    if plan:
        _ok(capsys, "section", "set", tid, "Plan", "-m", "1. do")


def test_epic_flow_via_cli(switch, ws_root, capsys):
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    _ok(capsys, "new", "--title", "Child", "--epic", "L-0001")
    _refine(capsys, "L-0002")
    _ok(capsys, "new", "--title", "Loose")
    _ok(capsys, "link", "L-0003", "--epic", "L-0001")
    shown = json.loads(_ok(capsys, "epic", "show", "L-0001", "--json"))
    assert [c["id"] for c in shown["children"]] == ["L-0002", "L-0003"]
    assert shown["charter"]["hash"].startswith("sha256:")
    _ok(capsys, "link", "L-0003", "--no-epic")
    switch.human("L-0001")
    out = _ok(capsys, "approve", "L-0001", "requirements", "--delegate", "--max-children", "3", "--max-size", "s")
    assert "L-0002" in out and "delegat" in out
    switch.agent()
    assert run(["link", "L-0003", "--epic", "L-0001"]) != 0  # approved epic: human only
    _ok(capsys, "new", "--title", "Auto", "--epic", "L-0001", "--size", "s")
    _refine(capsys, "L-0004")
    _ok(capsys, "epic", "auto-approve", "L-0004")
    shown = json.loads(_ok(capsys, "epic", "show", "L-0001", "--json"))
    states = {c["id"]: c["state"] for c in shown["children"]}
    assert states == {"L-0002": "covered", "L-0004": "delegated"}
    assert shown["delegation"]["active"] and shown["delegation"]["max_children"] == 3
    assert [a["ticket"] for a in shown["auto_approvals"]] == ["L-0004", "L-0004"]
    _ok(capsys, "claim", "L-0002")
    assert run(["epic", "pause", "L-0001"]) != 0  # human only
    switch.human("L-0001")
    _ok(capsys, "epic", "pause", "L-0001")
    shown = json.loads(_ok(capsys, "epic", "show", "L-0001", "--json"))
    assert shown["delegation"]["paused"]


def test_agent_cannot_delegate_via_cli(switch, ws_root, capsys):
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    assert run(["approve", "L-0001", "requirements", "--delegate"]) != 0
    assert store.resolve(__import__("orch.core.workspace", fromlist=["Workspace"]).Workspace.open(ws_root),
                         "L-0001").status == "backlog"


def test_sprint_cli(switch, ws_root, capsys):
    cfg = ws_root / "orchestrator" / "config.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["sprints"] = [{"id": "S1", "name": "Sprint 1", "start": "2000-01-01", "end": "2999-12-31"}]
    cfg.write_text(json.dumps(data), encoding="utf-8")
    rows = json.loads(_ok(capsys, "sprint", "list", "--json"))
    assert rows[0]["id"] == "S1"
    assert json.loads(_ok(capsys, "sprint", "current", "--json"))["id"] == "S1"
    _ok(capsys, "new", "--title", "Planned", "--sprint", "S1")
    _ok(capsys, "link", "L-0001", "--no-sprint")
    assert run(["link", "L-0001", "--sprint", "nope"]) != 0


def _ws(ws_root):
    from orch.core.workspace import Workspace
    return Workspace.open(ws_root)


def test_cli_shows_the_whole_charter_before_the_confirmation(switch, ws_root, capsys, monkeypatch):
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    _ok(capsys, "new", "--title", "Child one", "--epic", "L-0001")
    _ok(capsys, "section", "set", "L-0002", "Requirements", "-m", "export every invoice")
    _ok(capsys, "section", "set", "L-0002", "Acceptance criteria", "-m", "- [ ] opens in Excel")
    _ok(capsys, "section", "set", "L-0002", "Plan", "-m", "1. write the exporter")
    switch.human("L-0001")
    shown = {}

    def confirm(prompt=""):
        shown["before"] = capsys.readouterr().out  # what was printed before the typed confirmation
        return "L-0001"
    monkeypatch.setattr("builtins.input", confirm)
    assert run(["approve", "L-0001", "requirements", "--delegate", "--max-size", "l"]) == 0
    before = shown["before"]
    for text in ("Child one", "export every invoice", "opens in Excel", "write the exporter", "size: m",
                 "type: feature", "Delegation: on, up to 10 children of size ≤ l"):
        assert text in before, text
    entry = [x for x in __import__("orch.core.ledger", fromlist=["x"]).entries(_ws(ws_root)) if x["kind"] == "charter"][-1]
    assert entry["delegate"] == {"max_children": 10, "max_size": "l"}


def test_cli_refuses_a_charter_that_changed_before_the_confirmation(switch, ws_root, capsys, monkeypatch):
    from orch.core import ledger
    from orch.core.events import Actor
    from orch.core.ops import Ops
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    _ok(capsys, "new", "--title", "Child one", "--epic", "L-0001")
    _refine(capsys, "L-0002")
    switch.human("L-0001")

    def confirm(prompt=""):  # an agent adds a child while the human reads
        ws = _ws(ws_root)
        late = Ops(ws, Actor("agent", "x", "cli", "s-2")).new("Late", epic="L-0001")
        for name, text in (("Requirements", "r"), ("Acceptance criteria", "- [ ] a")):
            Ops(ws, Actor("agent", "x", "cli", "s-2")).set_section(late.id, name, text)
        return "L-0001"
    monkeypatch.setattr("builtins.input", confirm)
    assert run(["approve", "L-0001", "requirements"]) != 0
    ws = _ws(ws_root)
    assert store.resolve(ws, "L-0001").status == "backlog"
    assert not [x for x in ledger.entries(ws) if x["kind"] == "charter"]


def test_cli_epic_verdict_shows_the_evidence_and_binds_it(switch, ws_root, capsys, monkeypatch):
    from orch.core.events import Actor
    from orch.core.ops import Ops
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    _ok(capsys, "new", "--title", "Child one", "--epic", "L-0001")
    _refine(capsys, "L-0002")
    switch.human("L-0001")
    _ok(capsys, "approve", "L-0001", "requirements")
    switch.agent()
    _ok(capsys, "claim", "L-0002")
    _ok(capsys, "task", "add", "L-0002", "the work")
    _ok(capsys, "task", "start", "L-0002", "T1")
    _ok(capsys, "task", "done", "L-0002", "T1", "-m", "ok")
    _ok(capsys, "section", "set", "L-0002", "Verification", "-m", "- AC1: exported 3 invoices")
    _ok(capsys, "move", "L-0002", "testing")
    switch.human("L-0001")

    def changed(prompt=""):
        assert "exported 3 invoices" in capsys.readouterr().out
        ws = _ws(ws_root)
        Ops(ws, Actor("agent", "x", "cli", "s-1")).set_section("L-0002", "Verification", "- AC1: something else")
        return "L-0001"
    monkeypatch.setattr("builtins.input", changed)
    assert run(["verdict", "L-0001", "done"]) != 0
    assert store.resolve(_ws(ws_root), "L-0002").status == "testing"
    monkeypatch.setattr("builtins.input", lambda prompt="": "L-0001")
    _ok(capsys, "verdict", "L-0001", "done")
    assert store.resolve(_ws(ws_root), "L-0002").status == "done"


def test_epic_approve_dry_run_writes_nothing(switch, ws_root, capsys):
    from orch.core import ledger
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    _ok(capsys, "new", "--title", "Child", "--epic", "L-0001")
    _refine(capsys, "L-0002")
    switch.human("never typed")
    events_before = _events(_ws(ws_root))
    out = _ok(capsys, "approve", "L-0001", "requirements", "--delegate", "--dry-run")
    assert "would approve the epic" in out
    ws = _ws(ws_root)
    assert store.resolve(ws, "L-0001").status == "backlog" and store.resolve(ws, "L-0002").status == "backlog"
    assert not ledger.entries(ws)
    assert events_before == _events(ws)  # no gate.approved or status event for the epic or a child
    assert not list((ws.state_dir / "gates").glob("*.md"))  # no gate snapshot either


def _events(ws):
    log = ws.state_dir / "events.jsonl"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_epic_pause_dry_run_writes_nothing(switch, ws_root, capsys):
    from orch.core import ledger
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    switch.human("L-0001")
    _ok(capsys, "approve", "L-0001", "requirements", "--delegate")
    ws = _ws(ws_root)
    path = store.resolve(ws, "L-0001").path
    before = (path.read_bytes(), _events(ws))
    switch.human("never typed")
    out = _ok(capsys, "epic", "pause", "L-0001", "--dry-run")
    assert "would pause" in out and "nothing was written" in out
    assert (path.read_bytes(), _events(ws)) == before
    assert "pause" not in [e["kind"] for e in ledger.entries(ws)]
    assert not json.loads(_ok(capsys, "epic", "show", "L-0001", "--json"))["delegation"]["paused"]
    doc = json.loads(_ok(capsys, "epic", "pause", "L-0001", "--dry-run", "--json"))
    assert doc["dry_run"] is True


def test_epic_show_prints_the_content_hash_the_approval_shows(switch, ws_root, capsys):
    """Final review M2: the hash `orch epic show` prints is the one the approve prompt and the dashboard show."""
    from orch.core import epics
    _ok(capsys, "new", "--title", "Billing", "--type", "epic")
    _refine(capsys, "L-0001", plan=False)
    out = _ok(capsys, "epic", "show", "L-0001")
    ch = epics.charter(_ws(ws_root), store.load(_ws(ws_root), "L-0001")[1])
    assert ch["content_hash"][7:15] in out
    assert ch["hash"][7:15] not in out
