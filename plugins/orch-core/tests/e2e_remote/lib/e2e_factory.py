"""A workspace with the AI Factory on and a running epic, built the way the repo's own tests build one (the repo's fakes,
the real ledger and permits code): nothing here starts an agent or spends API credit. A child under the epic has asked
for a command permission; a second epic has a child in testing, ready for the owner's verdict."""
from __future__ import annotations

import json
from contextlib import contextmanager

import e2e_harness as H

COMMAND = "make deploy staging"
SESSION = "11111111-2222-3333-4444-555555555555"


@contextmanager
def human_in_process(state_dir):
    """ORCH_STATE_DIR set, and the two answers of the human-only checks the repo's tests replace (no agent ancestry, an
    interactive terminal), for the length of the build only."""
    import os
    import orch.actor as actor
    old = actor.process_chain, actor.is_interactive
    markers = {k: os.environ.pop(k) for k in H.AGENT_VARS if k in os.environ}   # a human action is refused under these
    actor.process_chain, actor.is_interactive = (lambda: []), (lambda: True)
    try:
        with H.state_env(state_dir):
            yield
    finally:
        actor.process_chain, actor.is_interactive = old
        os.environ.update(markers)


def enable_factory(host) -> None:
    path = host.root / "orchestrator" / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["factory"] = {"enabled": True}
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _refine(ops, tid: str, plan: str | None = "1. do it") -> None:
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    if plan:
        ops.set_section(tid, "Plan", plan)


def build(host) -> dict:
    """-> {"epic", "child", "permit"} (a running epic whose child asked for COMMAND) and {"ready_epic", "ready_child"}."""
    from orch.core import epics, factory_sessions, permits, store
    from orch.core.events import Actor
    from orch.core.gates import gate_hash
    from orch.core.ops import Ops
    from orch.core.workspace import Workspace

    with human_in_process(host.state_dir):
        ws = Workspace.open(host.root, use_env=False)
        agent, human = Actor("agent", "claude-code", "cli", "7f3c9a21-0000"), Actor("human", "you", "tty")
        fa = Ops(ws, agent)

        class HumanOps(Ops):
            def approve(self, ref, gate, **kw):
                if kw.get("expected_hash") is None:
                    t = store.load(ws, store.resolve(ws, ref).id)[1]
                    kw["expected_hash"] = epics.charter(ws, t)["content_hash"] if epics.is_epic(t) else gate_hash(t, gate)
                return super().approve(ref, gate, **kw)
        fh = HumanOps(ws, human)

        e = fa.new("Billing revamp", type="epic")
        _refine(fa, e.id, plan=None)
        fh.approve(e.id, "requirements", delegate={"factory": True})
        c = fa.new("child to deploy", epic=e.id)
        _refine(fa, c.id)
        fa.epic_auto_approve(c.id)
        fa.claim(c.id)
        d = epics.delegation(ws, store.load(ws, e.id)[1])
        factory_sessions.bind(ws, human, session=SESSION, epic=e.id, delegation=d["id"], child=c.id, name=f"fx-{c.id}",
                              wake="")
        factory_sessions.set_pid(ws, human, SESSION, __import__("os").getpid())   # a real process, as the runner records
        r = permits.request(ws, agent, store.load(ws, c.id)[1], COMMAND, reason="deploy the preview")
        out = {"epic": e.id, "child": c.id, "permit": r["id"]}

        e2 = fa.new("Invoice export", type="epic")
        _refine(fa, e2.id, plan=None)
        fh.approve(e2.id, "requirements", delegate={"factory": True})
        c2 = fa.new("export child", epic=e2.id)
        _refine(fa, c2.id)
        fa.epic_auto_approve(c2.id)
        fa.claim(c2.id)
        _, ids = fa.task_add(c2.id, [{"text": "the work"}])
        fa.task_start(c2.id, ids[0])
        fa.task_done(c2.id, ids[0])
        fa.set_section(c2.id, "Verification", "- AC1: ran the full suite, green")
        fa.set_section(c2.id, "Findings", "nothing left")
        fa.move(c2.id, "testing")
        out.update(ready_epic=e2.id, ready_child=c2.id)
        return out
