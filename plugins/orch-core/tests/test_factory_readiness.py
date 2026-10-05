"""AI Factory runner: readiness checks that run the sessions' real environment before anything starts (the live run of
5 Oct: hooks that could not run without uv, a `claude` wrapper that exited at once, the trust dialog, auto mode for
Haiku). Every program the checks run is a stand-in here; no check may write anything."""
import json
import os

import pytest

from orch.core import factory_runner as fr, factory_sessions as fs
from orch.dashboard import launch
from test_factory_runner import Fake, _started, _tick, fa, fh, fws  # noqa: F401  (fixtures)

REAL = fr.readiness  # conftest stands in for it in every other test
SETTINGS = {"factory_command": launch.DEFAULT_FACTORY_COMMAND}
BINS = {"claude": "/opt/claude/bin/claude", "env": "/usr/bin/env", "orch": "/opt/orch/bin/orch",
        "uv": "/opt/uv/bin/uv", "tmux": "/opt/tmux/bin/tmux"}


class Probe:
    """Stands in for every program a check runs; answers by what the argv runs."""
    def __init__(self):
        self.calls, self.answers = [], {}

    def __call__(self, argv, stdin, cwd, timeout=60):
        self.calls.append((argv, stdin, cwd))
        key = "version" if argv[-1] == "--version" else "guard" if "guard" in argv[-1] else "permit"
        return self.answers.get(key, (0, "2.1.0 (Claude Code)\n" if key == "version" else "", ""))


@pytest.fixture
def env(tmp_path, monkeypatch, ws):
    """A user config dir that passes every check, the programs resolved under /opt, and the probe."""
    d = tmp_path / "claude-user"
    (d / "skills" / "orch-work-on-ticket").mkdir(parents=True)
    (d / "skills" / "orch-work-on-ticket" / "SKILL.md").write_text("x", encoding="utf-8")
    settings = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "orch guard --hook-json"}]}],
                          "PermissionRequest": [{"hooks": [{"type": "command", "command": "orch permit hook"}]}]},
                "permissions": {"defaultMode": "acceptEdits", "deny": ["Artifact", "WebFetch", "WebSearch"]}}
    (d / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (d / ".claude.json").write_text(json.dumps({"projects": {str(ws.root.resolve()): {"hasTrustDialogAccepted": True}}}),
                                    encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    monkeypatch.setattr(fr, "resolve_bin", lambda name: BINS.get(os.path.basename(name)))
    monkeypatch.setattr(fr, "which", lambda name, path=None: BINS.get(name) if path and
                        os.path.dirname(BINS.get(name, "/x/x")) in path.split(os.pathsep) else None)
    probe = Probe()
    monkeypatch.setattr(fr, "_probe", probe)
    return {"dir": d, "settings": settings, "probe": probe}


def _failing(ws):
    return {c["name"]: c for c in REAL(ws, SETTINGS) if not c["ok"]}


def _write(env, **over):
    (env["dir"] / "settings.json").write_text(json.dumps({**env["settings"], **over}), encoding="utf-8")


def test_everything_passes_and_runs_under_the_sessions_own_environment(ws, env):
    before = sorted(p.name for p in env["dir"].rglob("*"))
    assert _failing(ws) == {}
    calls = env["probe"].calls
    assert [a[-1] for a, _, _ in calls] == ["--version", "orch guard --hook-json", "orch permit hook"]
    for argv, stdin, cwd in calls:
        assert argv[:2] == ["/usr/bin/env", "-i"] and argv[2].startswith("PATH=")
        path = argv[2][5:].split(":")
        assert path[:3] == ["/opt/claude/bin", "/opt/orch/bin", "/opt/uv/bin"] and "/usr/bin" in path
        assert cwd == str(ws.root.resolve())
    guard = calls[1]
    assert guard[0][-3:-1] == ["/bin/sh", "-c"] and json.loads(guard[1])["tool_name"] == "Bash"
    assert sorted(p.name for p in env["dir"].rglob("*")) == before  # nothing written


def test_a_hook_that_cannot_run_blocks_with_its_output(ws, env):
    env["probe"].answers["guard"] = (127, "", "orch: uv is not installed (https://docs.astral.sh/uv/); skipping.\n")
    f = _failing(ws)
    assert f["guard"]["level"] == "block" and "does not run under the sessions' environment (exit 127)" in f["guard"]["why"]
    assert "uv is not installed" in f["guard"]["tail"]
    env["probe"].answers["guard"] = (0, "not json", "")
    assert "guard" in _failing(ws)


def test_a_claude_wrapper_that_exits_at_once_blocks(ws, env):
    env["probe"].answers["version"] = (1, "", "claude not found in PATH\n")
    f = _failing(ws)
    assert "wrapper" in f["claude"]["why"] and "claude not found in PATH" in f["claude"]["tail"]


def test_orch_missing_from_the_session_path_blocks_and_says_how_to_fix(ws, env, monkeypatch):
    monkeypatch.setattr(fr, "resolve_bin", lambda name: {**BINS, "orch": None}.get(os.path.basename(name)))
    f = _failing(ws)
    assert "`orch` is not on the sessions' PATH" in f["orch on PATH"]["why"] and "uv tool install" in f["orch on PATH"]["why"]


def test_the_trust_dialog_blocks_until_accepted_here_or_above(ws, env):
    (env["dir"] / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    assert "Open Claude once in this folder" in _failing(ws)["trust"]["why"]
    (env["dir"] / ".claude.json").write_text(
        json.dumps({"projects": {str(ws.root.resolve().parent): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert "trust" not in _failing(ws)
    (env["dir"] / ".claude.json").unlink()
    assert "trust" in _failing(ws)


def test_skills_and_outward_tools_only_warn(ws, env, monkeypatch):
    import shutil
    shutil.rmtree(env["dir"] / "skills")
    _write(env, permissions={"defaultMode": "acceptEdits", "deny": ["WebFetch(domain:x.com)"]})
    f = _failing(ws)
    assert f["skills"]["level"] == f["outward tools"]["level"] == "warn"
    assert "Artifact, WebFetch, WebSearch" in f["outward tools"]["why"] and "never reach" in f["outward tools"]["why"]
    monkeypatch.setattr(fr, "readiness", REAL)
    assert fr.readiness_blocker(ws, SETTINGS) is None  # warnings start things


def test_plugin_hooks_are_found_and_run_from_the_installed_plugin(ws, env, tmp_path):
    root = tmp_path / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "orch").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/orch\" guard --hook-json"}]}],
        "PermissionRequest": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/orch\" permit hook"}]}]}}),
        encoding="utf-8")
    (env["dir"] / "plugins").mkdir()
    (env["dir"] / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"orch-core@orch": [{"scope": "user", "installPath": str(root)}]}}), encoding="utf-8")
    _write(env, hooks={}, enabledPlugins={"orch-core@orch": True})
    assert _failing(ws) == {}
    hooks = [a for a, _, _ in env["probe"].calls if a[-1] != "--version"]
    assert len(hooks) == 2 and all(f"CLAUDE_PLUGIN_ROOT={root}" in a for a in hooks)
    (env["dir"] / "plugins" / "installed_plugins.json").unlink()
    assert "cannot find the guard and permission hook" in _failing(ws)["hooks"]["why"]


def test_auto_mode_does_not_let_haiku_write_files(monkeypatch, env):
    _write(env, permissions={"defaultMode": "auto"})
    assert fr.edits_why(command=["claude", "--model", "sonnet"]) is None
    why = fr.edits_why(command=["claude", "--model", "claude-haiku-4-5"])
    assert why and "auto" in why and "haiku" in why and "acceptEdits" in why
    _write(env, permissions={"defaultMode": "auto"}, model="haiku")
    assert fr.edits_blocked(command=["claude"])
    _write(env, permissions={"defaultMode": "acceptEdits"}, model="haiku")
    assert not fr.edits_blocked(command=["claude", "--model", "haiku"])


def test_the_runner_starts_nothing_while_a_check_blocks(fws, fa, fh, human, env, monkeypatch):
    monkeypatch.setattr(fr, "readiness", REAL)
    (env["dir"] / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    fake = Fake()
    _started(fws, fa, fh)
    lines = _tick(fws, human, fake)
    assert not fake.started and any("not starting anything" in x and "trust dialog" in x for x in lines)
    from orch.core import store
    from orch.dashboard.data import factory as data
    eid = next(e.id for e in store.scan(fws) if e.meta and e.meta.get("type") == "epic")
    r = data.run_view(fws, store.load(fws, eid)[1])
    assert r["state"] == "blocked" and any(c["name"] == "trust" for c in r["checks"])
    assert fs.bindings(fws) == []
    (env["dir"] / ".claude.json").write_text(
        json.dumps({"projects": {str(fws.root.resolve()): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    fr._READY.clear()  # FAIL_TTL passed
    _tick(fws, human, fake)
    assert len(fake.started) == 1
    argv = fake.started[0][2]
    assert argv[2].startswith("PATH=/opt/claude/bin:/opt/orch/bin:/opt/uv/bin:")  # orch and uv reach the session
