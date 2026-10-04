"""Writes the fixtures orch-session's tests read: real `orch list --mine --json` and `orch show <id> --json` output
for one session holding a ticket in every move state, and the move orch-core's dashboard (orch.dashboard.data.cards)
gives each ticket, to check the CLI's `move` against.

Run it with orch-core's environment, from the repository root:

    uv run --project plugins/orch-core python plugins/orch-session/test/make_fixtures.py OUT_DIR

The workspace, the state dir and the config dirs are temporary; nothing outside them is read or written.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SESSION = "fixture-session-0001"
# The agent markers the orch-core test suite clears too (tests/conftest.py): the fixtures are built as the suite
# builds its workspaces, whoever runs this script.
AGENT_ENV_VARS = ("ORCH_HOME", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ORCH_HARNESS", "ORCH_SESSION", "ORCH_MODEL",
                  "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "GEMINI_CLI")
PR_URL = "https://github.com/acme/repo/pull/12"
CHECKED = "job ran: exit 0 and the output was checked"


def build(root: Path) -> dict[str, str]:
    import orch.actor
    from orch.core import epics, store
    from orch.core.events import Actor
    from orch.core.gates import gate_hash
    from orch.core.ops import Ops
    from orch.core.workspace import Workspace

    orch.actor.process_chain = lambda: []  # as tests/conftest.py: the caller's process tree does not decide who acts
    home = root / "orchestrator"
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"schema": 1, "customer": "acme", "id": {"prefix": "L", "pad": 4},
                                                  "gates": {"plan_skip_sizes": ["xs"]}}), encoding="utf-8")
    ws = Workspace.open(root)
    a = Ops(ws, Actor("agent", "claude-code", "cli", SESSION))
    other = Ops(ws, Actor("agent", "claude-code", "cli", "fixture-other-0002"))
    h = Ops(ws, Actor("human", "you", "tty"))

    def load(tid):
        return store.load(ws, tid)[1]

    def edit_meta(tid, **meta):  # fields no agent command sets (a PR of an unknown repo, blocked_by)
        path, t = store.load(ws, tid)
        t.meta.update(meta)
        store.save(ws, t, path)

    def approve(tid, gate):
        t = load(tid)
        h.approve(tid, gate, expected_hash=epics.charter(ws, t)["content_hash"] if epics.is_epic(t) else gate_hash(t, gate))

    def refine(tid, plan="1. do it"):
        a.set_section(tid, "Requirements", "r")
        a.set_section(tid, "Acceptance criteria", "- [ ] a\n- [ ] b")
        if plan:
            a.set_section(tid, "Plan", plan)

    def claimed(title, size="xs"):
        tid = a.new(title, size=size).id
        refine(tid)
        approve(tid, "requirements")
        a.claim(tid)
        return tid

    def tasks(tid, *items):
        return a.task_add(tid, [i if isinstance(i, dict) else {"text": i} for i in items])[1]

    def to_testing(tid):
        (t1,) = tasks(tid, "only")
        a.task_start(tid, t1)
        a.task_done(tid, t1)
        a.set_section(tid, "Verification", CHECKED)
        a.move(tid, "testing")

    out: dict[str, str] = {}
    w = claimed("Working ticket", size="m")
    approve(w, "plan")
    t1, t2, _ = tasks(w, "first", "second", "third")
    a.task_start(w, t1)
    a.task_done(w, t1)
    a.task_start(w, t2)
    edit_meta(w, prs=[{"repo": "repo", "url": PR_URL, "state": "draft"}])
    out["working"] = w

    q = claimed("Question ticket")
    (t1,) = tasks(q, "do")
    a.task_start(q, t1)
    a.ask(q, [{"text": "Which one?", "options": [{"key": "A", "label": "both"}, {"key": "B", "label": "only one"}],
               "recommended": "A"},
              {"text": "Non-blocking nicety?", "options": [{"key": "A", "label": "yes"}, {"key": "B", "label": "no"}],
               "recommended": "B", "blocking": False}])
    out["answer"] = q

    out["approve_plan"] = claimed("Plan ticket", size="m")

    r = claimed("Changed plan ticket", size="m")
    approve(r, "plan")
    a.set_section(r, "Plan", "1. do it differently")
    out["re_approve"] = r

    c = claimed("Changes ticket", size="m")
    h.request_changes(c, "plan", "smaller steps", expected_hash=gate_hash(load(c), "plan"))
    out["changes"] = c

    ht = claimed("Human task ticket")
    t1, _ = tasks(ht, "agent part", {"text": "press the button", "owner": "human"})
    a.task_start(ht, t1)
    a.task_done(ht, t1)
    out["human_task"] = ht

    rd = claimed("Ready ticket")
    (t1,) = tasks(rd, "only")
    a.task_start(rd, t1)
    a.task_done(rd, t1)
    out["ready"] = rd

    te = claimed("Testing ticket")
    to_testing(te)
    out["verdict"] = te

    fu = claimed("Follow-up ticket")
    to_testing(fu)
    h.verdict(fu, "follow-up", "missing b", expected_hash=epics.verdict_hash([load(fu)], ws))
    out["follow_up"] = fu

    out["no_tasks"] = claimed("No tasks ticket")

    bt = claimed("Blocked task ticket")
    t1, _ = tasks(bt, "one", "two")
    a.task_start(bt, t1)
    a.task_block(bt, t1, "waits on infra")
    out["blocked_task"] = bt

    dep = a.new("Dependency", size="xs").id
    bb = claimed("Blocked-by ticket")
    edit_meta(bb, blocked_by=[dep])
    out["blocked_by"] = bb

    e = a.new("Epic", type="epic").id
    refine(e, plan=None)
    ch = a.new("Epic child", size="xs", epic=e).id
    refine(ch, plan=None)
    approve(e, "requirements")
    a.claim(ch)
    out["epic_child"] = ch

    other_id = other.new("Other session", size="xs").id
    refine(other_id)
    approve(other_id, "requirements")
    other.claim(other_id)

    dn = claimed("Done ticket")
    to_testing(dn)
    h.verdict(dn, "done", expected_hash=epics.verdict_hash([load(dn)], ws))
    return out


def moves(root: Path) -> dict[str, dict]:
    from orch.core import store
    from orch.core.workspace import Workspace
    from orch.dashboard.data.cards import Cards
    ws = Workspace.open(root)
    cards = Cards(ws)
    out = {}
    for entry in store.scan(ws):
        m = cards.for_entry(entry)["move"]
        out[entry.id] = {"who": m["who"], "what": m["what"], "label": m["label"], "role": m["role"], "ref": m["ref"]}
    return out


def main(dest: Path) -> None:
    for var in AGENT_ENV_VARS:
        os.environ.pop(var, None)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for name in ("state", "xdg", "claude"):
            (tmp_path / name).mkdir()
        os.environ.update(ORCH_STATE_DIR=str(tmp_path / "state"), XDG_CONFIG_HOME=str(tmp_path / "xdg"),
                          CLAUDE_CONFIG_DIR=str(tmp_path / "claude"))
        root = tmp_path / "ws"
        root.mkdir()
        os.chdir(root)
        scenarios = build(root)

        orch = [sys.executable, "-c", "import sys; from orch.cli import run; sys.exit(run())"]

        def cli(*args, session=None):
            env = dict(os.environ, **({"CLAUDE_CODE_SESSION_ID": session} if session else {}))
            done = subprocess.run([*orch, *args], cwd=root, env=env, capture_output=True, text=True, check=True)
            return json.loads(done.stdout)

        dest.mkdir(parents=True, exist_ok=True)
        mine = cli("list", "--mine", "--json", session=SESSION)
        files = {"list-mine.json": mine, "scenarios.json": scenarios, "moves.json": moves(root)}
        files.update({f"show-{row['id']}.json": cli("show", row["id"], "--json", session=SESSION) for row in mine})
        for old in dest.glob("*.json"):
            old.unlink()
        for name, data in files.items():
            (dest / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {len(files)} fixtures to {dest}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
