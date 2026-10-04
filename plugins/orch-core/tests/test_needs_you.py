import io
import json

from orch.cli import run
from orch.core.query import needs_you
from orch.hooks.session_start import session_start_text


def kinds(ws):
    return [(i["ticket"], i["kind"]) for i in needs_you(ws)]


def test_needs_you_kinds_and_order(ws, aops, hops, put):
    ready = put("backlog", sections={"Requirements": "r", "Acceptance criteria": "- [ ] a"})
    put("backlog")  # nothing to approve yet
    q = {"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True,
         "answer": None, "note": None, "answered": None, "via": None}
    asking = put("waiting", questions=[q])
    planned = put("in-progress", sections={"Plan": "1. step"})
    xs = put("in-progress", size="xs", sections={"Plan": "1. step"})  # xs: no plan approval needed
    testing = put("testing")
    put("done")
    (ws.status_dir("open") / "L-0077-broken.md").write_text("---\nid: [\n---\n", encoding="utf-8")
    got = kinds(ws)
    assert got == [  # #11: blocking items first; requirements on an unclaimed backlog ticket are grooming, last
        ("L-0077", "broken"),
        (asking, "answer"),
        (planned, "approve-plan"),
        (testing, "verdict"),
        (ready, "approve-requirements"),
    ]
    assert all(t != xs for t, _ in got)


def test_needs_you_ignores_non_dict_questions(ws, aops, hops, put):
    q = {"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True,
         "answer": None, "note": None, "answered": None, "via": None}
    tid = put("waiting", questions=[None, "oops", q])
    got = needs_you(ws)
    assert kinds(ws) == [(tid, "answer")]
    assert got[0]["detail"] == "Q1"


def test_needs_you_reapprove_after_edit(ws, aops, hops):
    t = aops.new("x")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] a")
    hops.approve(t.id, "requirements")
    assert kinds(ws) == []
    aops.set_section(t.id, "Requirements", "changed")
    assert kinds(ws) == [(t.id, "re-approve")]
    assert needs_you(ws)[0]["detail"] == "requirements"


def test_ready_human_task_needs_you(ws, aops, working, plan_approved):
    aops.task_add(working, [{"text": "a"}, {"text": "grant", "owner": "human", "needs": ["T1"]},
                            {"text": "sign off", "owner": "human"}])
    plan_approved(working)
    assert kinds(ws) == [(working, "task")]
    assert needs_you(ws)[0]["detail"] == "T3"
    aops.task_start(working, "T1")
    aops.task_done(working, "T1")
    assert [i["detail"] for i in needs_you(ws) if i["kind"] == "task"] == ["T2", "T3"]


def test_task_items_sort_after_answers(ws, aops, hops, working, put):
    q = {"id": "Q1", "text": "?", "type": "text", "options": [], "blocking": True,
         "answer": None, "note": None, "answered": None, "via": None}
    asking = put("waiting", questions=[q])
    aops.task_add(working, [{"text": "grant", "owner": "human"}])
    assert [k for _, k in kinds(ws)] == ["answer", "task"]


def test_blocked_human_task_does_not_need_you(ws, aops, hops, working):
    aops.task_add(working, [{"text": "grant", "owner": "human"}])
    hops.task_block(working, "T1", "waits for the platform team", on="DEMO-0042")
    assert kinds(ws) == []


def test_broken_tasks_section_is_a_repair_item(ws, put):
    tid = put("in-progress", sections={"Tasks": "nonsense"})
    assert kinds(ws) == [(tid, "broken")]
    assert needs_you(ws)[0]["detail"].startswith("Tasks line 1")


def test_session_start_text(ws, aops, put):
    tid = put("open")
    aops.claim(tid)
    put("testing")
    text = session_start_text(ws, "7f3c9a21-0000")
    assert "agent may commit: no" in text
    assert f"Tickets claimed by this session: {tid} (in-progress, no tasks yet)" in text
    assert "Waiting on the human: 1 blocking" in text and "give a verdict" in text


def test_cli_session_start_reads_session_from_stdin(ws_root, put, monkeypatch, capsys):
    from orch.core.ops import Ops
    from orch.core.events import Actor
    from orch.core.workspace import Workspace
    tid = put("open")
    Ops(Workspace.open(ws_root), Actor("agent", "claude-code", "cli", "sess-from-hook")).claim(tid)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "sess-from-hook", "source": "startup"})))
    assert run(["hook", "session-start"]) == 0
    assert f"{tid} (in-progress, no tasks yet)" in capsys.readouterr().out


def test_cli_session_start_outside_workspace(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run(["hook", "session-start"]) == 0
    assert capsys.readouterr().out == ""


def test_init_writes_instructions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--customer", "initech", "--prefix", "ddp", "--harness", "claude", "--harness", "copilot"]) == 0
    assert (tmp_path / "AGENTS.md").exists() and (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / ".github" / "copilot-instructions.md").exists()
    assert (tmp_path / ".agents" / "skills" / "orch-tickets" / "SKILL.md").exists()
    cfg = json.loads((tmp_path / "orchestrator" / "config.json").read_text(encoding="utf-8"))
    assert cfg["harnesses"] == ["claude", "copilot"]
    ignore = (tmp_path / "orchestrator" / ".gitignore").read_text(encoding="utf-8")
    assert ".state/guard-errors.log" in ignore and ".state/addon-errors.log" in ignore


def test_init_without_instructions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert run(["init", "--customer", "x", "--no-instructions"]) == 0
    assert not (tmp_path / "AGENTS.md").exists()


def test_needs_you_is_cached_until_a_ticket_file_changes(ws, aops, put, monkeypatch):
    from orch.core import store
    tid = put("testing", title="Check export")
    other = put("open", title="Other")
    assert kinds(ws) == [(tid, "verdict")]
    parsed = []
    original = store.parse_ticket  # every ticket read goes through the store's parse cache
    monkeypatch.setattr(store, "parse_ticket", lambda *a, **k: parsed.append(1) or original(*a, **k))
    assert kinds(ws) == [(tid, "verdict")]
    assert parsed == []  # nothing changed: no ticket re-parsed
    aops.ask(other, [{"text": "Which env?", "options": ["dev", "prod"]}])
    assert (other, "answer") in kinds(ws)
    assert parsed  # the changed files were read again
