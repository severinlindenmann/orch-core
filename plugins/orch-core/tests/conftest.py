import json

import pytest

AGENT_ENV_VARS = ("ORCH_HOME", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ORCH_HARNESS", "ORCH_SESSION", "ORCH_MODEL",
                  "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "GEMINI_CLI")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path_factory):
    for var in AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path_factory.mktemp("orch-state")))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    # User-level Claude settings and the plugin root derived from the package must not leak the
    # developer's machine (or this store checkout) into doctor results; tests opt back in.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
    import orch.onboarding
    monkeypatch.setattr(orch.onboarding, "_package_plugin_root", lambda: None)
    # The suite may itself run under an agent harness (or CI): its real process tree must not decide who acts.
    # Tests that exercise ancestry detection replace this with their own chain.
    import orch.actor
    monkeypatch.setattr(orch.actor, "process_chain", lambda: [])


@pytest.fixture(autouse=True)
def _factory_pane_pid(monkeypatch):
    """The AI Factory hook trusts a session binding only for a process under the pid the runner recorded; tests that
    bind a session record 4242 and run "under" it, and say otherwise by replacing this themselves."""
    import orch.core.factory_sessions as fs
    monkeypatch.setattr(fs, "chain_pids", lambda: {4242})
    monkeypatch.setattr(fs, "proc_start", lambda pid: "Mon Oct  4 10:00:00 2026")


@pytest.fixture(autouse=True)
def _factory_ready(monkeypatch):
    """The runner's readiness checks run real programs (claude --version, the hooks) and read the user's Claude files:
    tests pass them unless they test them (tests/test_factory_readiness.py)."""
    import orch.core.factory_runner as fr
    monkeypatch.setattr(fr, "readiness", lambda ws, settings, environ=None: [])
    fr._READY.clear()


@pytest.fixture(autouse=True)
def _no_real_launch(monkeypatch, tmp_path_factory, _clean_env):
    """Start agent must never open a real terminal from a test: Popen in orch.dashboard.launch
    raises unless the test patches it itself (launch.subprocess is launch's own namespace, so
    this does not touch the subprocess module git calls use). The terminal the suite runs in
    (cmux, iTerm, ...) must not decide `auto` either."""
    import os
    import orch.dashboard.launch as launch

    def _refuse(*args, **kwargs):
        raise AssertionError("unexpected launch")

    monkeypatch.setattr(launch.subprocess, "Popen", _refuse)
    # launcher binaries (open, cmux, ...) "exist" so preflight passes on any CI runner
    monkeypatch.setattr(launch, "which", lambda name: f"/usr/bin/{name}")
    for var in [v for v in os.environ if v.startswith("CMUX_")] + ["TERM_PROGRAM"]:
        monkeypatch.delenv(var, raising=False)
    # the user-level config home (launch.json) is a temp dir; the real ~/.config is never read
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("xdg-config")))


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path_factory, monkeypatch):
    """git in tests must not read the user's global or system config (hooksPath, templates, signing...)."""
    empty = tmp_path_factory.mktemp("gitconfig") / "global"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def _forget_addon_modules():
    """Addon modules are cached in sys.modules by name (`_orch_addon_<name>_<digest hash>`); two tests with the
    same addon content would otherwise share one module, so each test starts without any."""
    import sys
    yield
    for name in [m for m in sys.modules if m.startswith("_orch_addon_")]:
        sys.modules.pop(name, None)


def make_config(**over):
    from orch.config.load import deep_merge
    return deep_merge({"schema": 1, "customer": "acme", "id": {"prefix": "L", "pad": 4}}, over)


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    root = tmp_path / "my workspace"  # space on purpose: iCloud paths contain spaces
    home = root / "orchestrator"
    home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps(make_config()), encoding="utf-8")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def ws(ws_root):
    from orch.core.workspace import Workspace
    return Workspace.open(ws_root)


@pytest.fixture
def configure(ws_root):
    def _configure(**over):
        from orch.core.workspace import Workspace
        (ws_root / "orchestrator" / "config.json").write_text(json.dumps(make_config(**over)), encoding="utf-8")
        return Workspace.open(ws_root)
    return _configure


@pytest.fixture
def agent():
    from orch.core.events import Actor
    return Actor("agent", "claude-code", "cli", "7f3c9a21-0000")


@pytest.fixture
def other_agent():
    from orch.core.events import Actor
    return Actor("agent", "copilot", "cli", "b2e4d6f8-0000")


@pytest.fixture
def human():
    from orch.core.events import Actor
    return Actor("human", "you", "tty")


@pytest.fixture
def html_on(ws, human):
    """Agent HTML in widgets turned on the human's way: a signed ledger entry plus the config (#9)."""
    from orch.core.ops import Ops
    Ops(ws, human).set_widgets_html(True)


@pytest.fixture
def aops(ws, agent):
    from orch.core.ops import Ops
    return Ops(ws, agent)


def seen_hash(ws, kind: str, ref: str, arg=None) -> str:
    """The hash a human decision binds, as the dashboard or the CLI shows it right now: `kind` approve (arg: the
    gate; an epic: its charter's content hash), answer (arg: the question id) or verdict."""
    from orch.core import epics, store
    from orch.core.gates import gate_hash
    from orch.core.questions import find_question, question_hash
    t = store.load(ws, store.resolve(ws, ref).id)[1]
    if kind == "answer":
        return question_hash(find_question(t, arg))
    if kind == "approve":
        return epics.charter(ws, t)["content_hash"] if epics.is_epic(t) else gate_hash(t, arg)
    return epics.verdict_hash(epics.open_children(ws, t) if epics.is_epic(t) else [t], ws)


def human_ops(ws, actor, **kw):
    """An Ops for a human who reads before deciding: approve, answer and verdict fill in the hash of what is on
    screen now when a test leaves `expected_hash` out (Ops itself requires one)."""
    from orch.core.ops import Ops

    class HumanOps(Ops):
        def approve(self, ref, gate, **kw):
            if kw.get("expected_hash") is None:
                try:
                    kw["expected_hash"] = seen_hash(ws, "approve", ref, gate)
                except Exception:  # let Ops report the real problem (unknown ticket, ...)
                    kw["expected_hash"] = "sha256:unknown"
            return super().approve(ref, gate, **kw)

        def answer(self, ref, qid, value, note=None, **kw):
            if kw.get("expected_hash") is None:
                try:
                    kw["expected_hash"] = seen_hash(ws, "answer", ref, qid)
                except Exception:
                    kw["expected_hash"] = "sha256:unknown"
            return super().answer(ref, qid, value, note, **kw)

        def verdict(self, ref, verdict, message=None, **kw):
            if kw.get("expected_hash") is None:
                try:
                    kw["expected_hash"] = seen_hash(ws, "verdict", ref)
                except Exception:
                    kw["expected_hash"] = "sha256:unknown"
            return super().verdict(ref, verdict, message, **kw)

    return HumanOps(ws, actor, **kw)


@pytest.fixture
def hops(ws, human):
    return human_ops(ws, human)


@pytest.fixture
def working(aops, hops):
    """A ticket claimed by `aops`, in progress, requirements approved, two acceptance criteria."""
    t = aops.new("Migrate jobs")
    aops.set_section(t.id, "Requirements", "r")
    aops.set_section(t.id, "Acceptance criteria", "- [ ] every job on serverless\n- [ ] cost compared")
    hops.approve(t.id, "requirements")
    aops.claim(t.id)
    return t.id


@pytest.fixture
def plan_approved(aops):
    """The human approves the ticket's plan in the dashboard (#9: an agent starts and finishes tasks only after
    that). Usable in tests whose process plays an agent: the agent markers are lifted for the approval only (a human
    action is refused while they are set)."""
    def _approve(tid, plan="1. do the work"):
        from orch.core import store
        from orch.core.events import Actor
        from orch.core.ops import Ops
        if not store.load(aops.ws, tid)[1].section("Plan").strip():
            aops.set_section(tid, "Plan", plan)
        with pytest.MonkeyPatch.context() as mp:
            for var in AGENT_ENV_VARS:
                mp.delenv(var, raising=False)
            human_ops(aops.ws, Actor("human", "you", "dashboard")).approve(tid, "plan")
    return _approve


@pytest.fixture
def close_tasks():
    """Give a claimed ticket one finished task, so it may move to testing (MC2-T rule)."""
    def _close(ops, tid, text="the work"):
        _, ids = ops.task_add(tid, [{"text": text}])
        ops.task_start(tid, ids[0])
        ops.task_done(tid, ids[0])
    return _close


@pytest.fixture
def put(ws):
    """Write a ticket straight to disk in any state (bypasses Ops and events)."""
    def _put(status="backlog", **meta):
        from orch.clock import stamp
        from orch.core import store
        from orch.core.ids import next_id
        from orch.core.model import new_ticket

        t = new_ticket(next_id(ws), meta.pop("title", "Test ticket"), type="feature", priority="normal",
                       size=meta.pop("size", "m"), created=stamp())
        t.meta["status"] = status
        for name, text in meta.pop("sections", {}).items():
            t.set_section(name, text)
        t.meta.update(meta)
        store.save(ws, t)
        return t.id
    return _put


@pytest.fixture
def dash(ws):
    """A dashboard TestClient that already holds the auth cookie."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    client = TestClient(create_app(ws, "tok"))
    assert client.get("/?token=tok").status_code == 200
    return client


@pytest.fixture
def wurl(ws):
    """The frame address the page would give a ticket's block, found by its id or index (test lookup only: the route
    itself addresses blocks by section and digest)."""
    def _url(tid, key):
        from orch.core import store
        from orch.widgets import ticket_blocks
        from orch.widgets.render import frame_path
        path = store.resolve(ws, tid).path
        blocks = ticket_blocks(store.read_ticket(path), path.read_text(encoding="utf-8"))
        block = next(b for b in blocks if b.key == key or str(b.index) == key)
        return frame_path(tid, block)
    return _url
