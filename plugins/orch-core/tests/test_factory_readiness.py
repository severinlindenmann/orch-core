"""AI Factory runner: readiness checks that run the sessions' real environment before anything starts (the live run of
5 Oct: hooks that could not run without uv, a `claude` wrapper that exited at once, the trust dialog, auto mode for
Haiku). Most programs the checks run are stand-ins; a few tests run small scripts of their own through the real probe.
No check may write anything, and no program inside the workspace is ever run or put on a session's PATH."""
import json
import os
import subprocess

import pytest

from orch.core import factory_runner as fr, factory_sessions as fs
from orch.dashboard import launch
from test_factory_runner import Fake, _started, _tick, fa, fh, fws  # noqa: F401  (fixtures)

pytestmark = pytest.mark.real_readiness  # conftest stubs readiness everywhere else
REAL_RESOLVE = fr.resolve_bin
SETTINGS = {"factory_command": launch.DEFAULT_FACTORY_COMMAND}
BINS = {"claude": "/opt/claude/bin/claude", "env": "/usr/bin/env", "orch": "/opt/orch/bin/orch",
        "uv": "/opt/uv/bin/uv", "tmux": "/opt/tmux/bin/tmux"}


class Probe:
    """Stands in for every program a check runs; answers by what the argv runs."""
    def __init__(self):
        self.calls, self.answers = [], {}

    def __call__(self, argv, stdin, cwd, timeout=60):
        self.calls.append((argv, stdin, cwd))
        key = "version" if argv[-1] == "--version" else "guard" if "guard" in argv else "permit"
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
    bins = dict(BINS)
    monkeypatch.setattr(fr, "resolve_bin", lambda name: bins.get(os.path.basename(name)))
    monkeypatch.setattr(fr, "which", lambda name, path=None: bins.get(name) if path and bins.get(name) and
                        os.path.dirname(bins[name]) in path.split(os.pathsep) else None)
    probe = Probe()
    monkeypatch.setattr(fr, "_probe", probe)
    return {"dir": d, "settings": settings, "probe": probe, "bins": bins}


def _failing(ws):
    return {c["name"]: c for c in fr.readiness(ws, SETTINGS) if not c["ok"]}


def _write(env, **over):
    (env["dir"] / "settings.json").write_text(json.dumps({**env["settings"], **over}), encoding="utf-8")


def test_everything_passes_and_runs_under_the_sessions_own_environment(ws, env):
    before = sorted(p.name for p in env["dir"].rglob("*"))
    assert _failing(ws) == {}
    calls = env["probe"].calls
    assert [a[-1] for a, _, _ in calls] == ["--version", "--hook-json", "hook"]
    assert calls[1][0][-3:] == ["orch", "guard", "--hook-json"] and calls[2][0][-3:] == ["orch", "permit", "hook"]
    for argv, stdin, cwd in calls:
        assert argv[:2] == ["/usr/bin/env", "-i"] and argv[2].startswith("PATH=")
        assert "/bin/sh" not in argv  # hook commands are parsed into words, never run through a shell
        path = argv[2][5:].split(":")
        assert path[:3] == ["/opt/claude/bin", "/opt/orch/bin", "/opt/uv/bin"] and "/usr/bin" in path
        assert cwd == str(ws.root.resolve())
    assert json.loads(calls[1][1])["tool_name"] == "Bash"
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


def test_orch_missing_from_the_session_path_blocks_and_says_how_to_fix(ws, env):
    env["bins"]["orch"] = None
    f = _failing(ws)
    assert "`orch` is not on the sessions' PATH" in f["orch on PATH"]["why"] and "uv tool install" in f["orch on PATH"]["why"]


@pytest.mark.parametrize("name", ["orch", "uv", "claude"])
def test_a_program_inside_the_workspace_blocks_and_never_reaches_a_session(fws, fa, fh, human, env, name):
    inside = str(fws.root / ".venv" / "bin" / name)
    env["bins"][name] = inside
    f = _failing(fws)
    assert "lies inside the workspace, which agents write" in f["programs"]["why"] and inside in f["programs"]["why"]
    assert not any(inside in a or os.path.dirname(inside) in a[2] for a, _, _ in env["probe"].calls)
    assert all(os.path.dirname(inside) not in b for b in fr.session_bins(fws))
    fake = Fake()
    _started(fws, fa, fh)
    lines = _tick(fws, human, fake)
    assert not fake.started and fs.bindings(fws) == [] and any("not starting anything" in x for x in lines)


def test_launch_refuses_claude_inside_the_workspace_even_without_readiness(fws, fa, fh, human, env, monkeypatch):
    monkeypatch.setattr(fr, "readiness", lambda ws, settings, environ=None: [])
    env["bins"]["claude"] = str(fws.root / "bin" / "claude")
    fake = Fake()
    _started(fws, fa, fh)
    assert any("inside the workspace" in x for x in _tick(fws, human, fake)) and not fake.started


def test_the_trust_dialog_blocks_until_accepted_here_or_above(ws, env):
    (env["dir"] / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    assert "Open Claude once in this folder" in _failing(ws)["trust"]["why"]
    (env["dir"] / ".claude.json").write_text(
        json.dumps({"projects": {str(ws.root.resolve().parent): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert "trust" not in _failing(ws)
    (env["dir"] / ".claude.json").unlink()
    assert "trust" in _failing(ws)


def test_trust_compares_real_paths_and_says_when_the_file_is_too_big(ws, env, tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    alias.symlink_to(ws.root)
    (env["dir"] / ".claude.json").write_text(
        json.dumps({"projects": {str(alias): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    assert "trust" not in _failing(ws)  # the key is written another way, the folder is the same
    monkeypatch.setattr(fr, "CLAUDE_JSON_CAP", 10)
    assert "too big for orch to read" in _failing(ws)["trust"]["why"]


def test_skills_and_outward_tools_only_warn(ws, env):
    import shutil
    shutil.rmtree(env["dir"] / "skills")
    _write(env, permissions={"defaultMode": "acceptEdits", "deny": ["WebFetch(domain:x.com)"]})
    f = _failing(ws)
    assert f["skills"]["level"] == f["outward tools"]["level"] == "warn"
    assert "Artifact, WebFetch, WebSearch" in f["outward tools"]["why"] and "never reach" in f["outward tools"]["why"]
    assert fr.readiness_blocker(ws, SETTINGS) is None  # warnings start things


def _plugin(tmp_path, where, hooks=None):
    root = where / "plugin"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "orch").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks or {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/orch\" guard --hook-json"}]}],
        "PermissionRequest": [{"hooks": [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/bin/orch\" permit hook"}]}]}}),
        encoding="utf-8")
    return root


def _install(env, root):
    (env["dir"] / "plugins").mkdir(exist_ok=True)
    (env["dir"] / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"orch-core@orch": [{"scope": "user", "installPath": str(root)}]}}), encoding="utf-8")
    _write(env, hooks={}, enabledPlugins={"orch-core@orch": True})


def test_plugin_hooks_are_found_and_run_from_the_installed_plugin_only(ws, env, tmp_path, monkeypatch):
    root = _plugin(tmp_path, tmp_path)
    _install(env, root)
    assert _failing(ws) == {}
    hooks = [a for a, _, _ in env["probe"].calls if a[-1] != "--version"]
    assert len(hooks) == 2 and all(f"CLAUDE_PLUGIN_ROOT={root}" in a and str(root / "bin" / "orch") in a for a in hooks)
    (env["dir"] / "plugins" / "installed_plugins.json").unlink()
    from orch import onboarding
    monkeypatch.setattr(onboarding, "_package_plugin_root", lambda: root)  # the package's own root is never probed
    assert "cannot find the guard and permission hook" in _failing(ws)["hooks"]["why"]


def test_a_plugin_inside_the_workspace_is_never_run(ws, env, tmp_path):
    _install(env, _plugin(tmp_path, ws.root))
    calls = env["probe"].calls
    assert "cannot find" in _failing(ws)["hooks"]["why"]
    assert not any(str(ws.root) in " ".join(a[3:]) for a, _, _ in calls if a[-1] != "--version")


def test_hook_commands_are_matched_by_words_not_text(ws, env, tmp_path):
    evil = {"PreToolUse": [{"hooks": [{"type": "command", "command": "sh -c 'touch /tmp/x' # guard --hook-json"}]}],
            "PermissionRequest": [{"hooks": [{"type": "command",
                                              "command": "/usr/bin/true permit hook \"${CLAUDE_PLUGIN_ROOT}/bin/orch\""}]}]}
    _install(env, _plugin(tmp_path, tmp_path, evil))
    assert "cannot find the guard and permission hook" in _failing(ws)["hooks"]["why"]
    assert [a[-1] for a, _, _ in env["probe"].calls] == ["--version"]


def test_auto_mode_does_not_let_haiku_write_files(env):
    _write(env, permissions={"defaultMode": "auto"})
    assert fr.edits_why(command=["claude", "--model", "sonnet"]) is None
    why = fr.edits_why(command=["claude", "--model", "claude-haiku-4-5"])
    assert why and "auto" in why and "haiku" in why and "acceptEdits" in why
    _write(env, permissions={"defaultMode": "auto"}, model="haiku")
    assert fr.edits_blocked(command=["claude"])
    _write(env, permissions={"defaultMode": "acceptEdits"}, model="haiku")
    assert not fr.edits_blocked(command=["claude", "--model", "haiku"])


def test_the_runner_starts_nothing_while_a_check_blocks(fws, fa, fh, human, env):
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
    fr._READY.clear()  # READY_TTL passed
    _tick(fws, human, fake)
    assert len(fake.started) == 1
    argv = fake.started[0][2]
    assert argv[2].startswith("PATH=/opt/claude/bin:/opt/orch/bin:/opt/uv/bin:")  # orch and uv reach the session


def test_a_readiness_that_raises_blocks_through_tick(fws, fa, fh, human, env, monkeypatch):
    def boom(ws, settings, environ=None):
        raise RuntimeError("broken")
    monkeypatch.setattr(fr, "readiness", boom)
    fake = Fake()
    _started(fws, fa, fh)
    lines = _tick(fws, human, fake)
    assert not fake.started and any("the readiness checks failed (RuntimeError)" in x for x in lines)
    assert fr.readiness_report(fws)[0]["name"] == "readiness"


def test_a_cached_pass_counts_only_while_the_programs_and_hooks_are_the_same(ws, env, monkeypatch):
    runs = []
    monkeypatch.setattr(fr, "readiness", lambda w, s, environ=None: runs.append(1) or [])
    assert fr.readiness_blocker(ws, SETTINGS) is None and fr.readiness_blocker(ws, SETTINGS) is None
    assert len(runs) == 1  # cached
    _write(env, hooks={"PreToolUse": [{"hooks": [{"type": "command", "command": "orch guard --other"}]}]})
    fr.readiness_blocker(ws, SETTINGS)
    env["bins"]["uv"] = "/opt/uv2/bin/uv"
    fr.readiness_blocker(ws, SETTINGS)
    assert len(runs) == 3  # a changed hook command or program is probed again
    assert fr.READY_TTL <= 60


# -- the real probe, with small programs of the test's own (outside the workspace) ------------------------------------

def _script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_the_real_probe_keeps_exit_status_and_a_capped_output(tmp_path):
    s = _script(tmp_path / "p" / "fails", "echo oops >&2\nexit 3\n")
    assert fr._probe([s], "", str(tmp_path)) == (3, "", "oops\n")
    big = _script(tmp_path / "p" / "big", "head -c 300000 /dev/zero | tr '\\0' x\n")
    code, out, _ = fr._probe([big], "", str(tmp_path))
    assert code == 0 and len(out) == fr.PROBE_CAP
    slow = _script(tmp_path / "p" / "slow", "sleep 20\n")
    import time
    t = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        fr._probe([slow], "", str(tmp_path), 0.5)
    assert time.monotonic() - t < 5


def test_a_failing_hook_through_the_real_probe(ws, tmp_path, monkeypatch):
    d = tmp_path / "claude-user"
    d.mkdir()
    hook = _script(tmp_path / "tools" / "orch", "cat >/dev/null\necho 'orch: uv is not installed; skipping.' >&2\n"
                                                  "exit 127\n")
    claude = _script(tmp_path / "tools" / "claude", "echo '2.1.0 (Claude Code)'\n")
    (d / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": f"{hook} guard --hook-json"}]}],
        "PermissionRequest": [{"hooks": [{"type": "command", "command": f"{hook} permit hook"}]}]}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    bins = {"claude": claude, "env": "/usr/bin/env"}
    monkeypatch.setattr(fr, "resolve_bin", lambda name: bins.get(os.path.basename(name)))
    f = _failing(ws)
    assert "claude" not in f  # the real `--version` of the stand-in claude passed
    assert "exit 127" in f["guard"]["why"] and "uv is not installed" in f["guard"]["tail"]
    assert "exit 127" in f["permission hook"]["why"]


def test_the_time_budget_covers_every_probe(ws, env, monkeypatch):
    monkeypatch.setattr(fr, "PROBE_BUDGET", 0.0)
    f = _failing(ws)
    assert "time budget" in f["claude"]["tail"] and env["probe"].calls == []


def test_a_folder_others_can_write_is_never_trusted(tmp_path, monkeypatch):
    s = _script(tmp_path / "shared" / "orch", "exit 0\n")
    (tmp_path / "shared").chmod(0o700)
    assert REAL_RESOLVE(s) == s
    (tmp_path / "shared").chmod(0o777)
    try:
        assert REAL_RESOLVE(s) is None
    finally:
        (tmp_path / "shared").chmod(0o700)


# -- the guard keeps agents off the files these checks read ------------------------------------------------------------

def test_the_guard_protects_the_user_scope_claude_files_and_installed_plugins(ws, tmp_path, monkeypatch):
    from pathlib import Path
    from orch.hooks.guard import evaluate
    d = Path(os.environ["CLAUDE_CONFIG_DIR"])
    plugin = tmp_path / "elsewhere" / "orch-core"
    (d / "plugins").mkdir(parents=True, exist_ok=True)
    (d / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"orch-core@orch": [{"installPath": str(plugin)}]}}), encoding="utf-8")

    def allowed(tool, inp):
        return evaluate(ws, {"tool_name": tool, "tool_input": inp, "cwd": str(ws.root)}).allow
    for p in (d / "settings.json", d / "settings.local.json", d / ".claude.json", d / "plugins" / "x" / "hooks.json",
              plugin / "hooks" / "hooks.json", Path.home() / ".claude" / "settings.json", Path.home() / ".claude.json"):
        assert not allowed("Write", {"file_path": str(p), "content": "{}"}), p
        assert not allowed("Edit", {"file_path": str(p), "old_string": "a", "new_string": "b"}), p
        assert not allowed("Bash", {"command": f"echo x > '{p}'"}), p
    for cmd in ("echo x > ~/.claude/settings.json", "cp a ~/.claude.json", "tee $HOME/.claude/plugins/p/hooks/hooks.json",
                "echo x > $CLAUDE_CONFIG_DIR/settings.json"):
        assert not allowed("Bash", {"command": cmd}), cmd
    for p in (ws.root / ".claude" / "notes.md", ws.root / "plugins" / "x" / "hooks" / "hooks.json"):
        assert allowed("Write", {"file_path": str(p), "content": "x"}), p  # nothing else
    assert allowed("Bash", {"command": "cat ~/.claude/settings.json"})  # reading stays open


def _shell_allowed(ws, cmd):
    from orch.hooks.guard import evaluate
    return evaluate(ws, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(ws.root)}).allow


@pytest.mark.parametrize("cmd", [
    'echo x > "$HOME"/.claude/settings.json', "cd ~/.claude && echo x > settings.json",
    "sed -i '' s/a/b/ ~/'.claude'/settings.json", "cd ~/.claude; rm -rf plugins", 'cp a "${HOME}/.claude.json"',
    "echo x > ~/.claude/hooks/pre.sh", "rm -rf ~/.claude/skills", "touch ~/.claude/agents/x.md",
    "echo x >> ~/.claude/CLAUDE.md", "pushd ~/.claude/plugins; touch x", "rm -rf ~/.claude",
    "echo x > \\~/.claude/settings.json",
])
def test_the_guard_sees_through_spellings_of_the_users_claude_files(ws, cmd):
    assert not _shell_allowed(ws, cmd), cmd


@pytest.mark.parametrize("cmd", [
    "echo x > notes.md; cat ~/.claude/settings.json",  # the write is elsewhere; reading stays open
    "echo '{}' > .claude/settings.json", "mkdir -p .claude && echo x > .claude/settings.local.json",
    "cat ~/.claude/settings.json", "ls ~/.claude/skills", "cd ~/.claude && cat settings.json",
])
def test_the_guard_does_not_over_block(ws, cmd):
    assert _shell_allowed(ws, cmd), cmd


def test_the_users_hooks_skills_agents_and_claude_md_and_orchs_programs_are_protected(ws, tmp_path, monkeypatch):
    from pathlib import Path
    from orch.hooks import guard
    from orch.hooks.guard import evaluate
    d = Path(os.environ["CLAUDE_CONFIG_DIR"])
    tool = tmp_path / "tools" / "orch-core"
    (tool / "bin").mkdir(parents=True)
    (tool / "pyvenv.cfg").write_text("x", encoding="utf-8")
    _script(tool / "bin" / "orch", "exit 0\n")
    link_dir = tmp_path / "localbin"
    link_dir.mkdir()
    (link_dir / "orch").symlink_to(tool / "bin" / "orch")
    monkeypatch.setenv("PATH", f"{link_dir}:/usr/bin:/bin")
    for p in (d / "hooks" / "x.sh", d / "skills" / "s" / "SKILL.md", d / "agents" / "a.md", d / "CLAUDE.md",
              Path.home() / ".claude" / "CLAUDE.md", link_dir / "orch", tool / "bin" / "orch",
              tool / "lib" / "orch" / "guard.py"):
        assert not evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(p), "content": "x"},
                                 "cwd": str(ws.root)}).allow, p
    assert not _shell_allowed(ws, f"echo x > {tool}/lib/orch/hooks/guard.py")
    inside = ws.root / ".venv" / "bin"  # an orch in the workspace is the workspace's (and readiness blocks it)
    folders = guard._harness_targets(ws)[1]
    assert str(inside).lower() not in folders
    assert evaluate(ws, {"tool_name": "Write", "tool_input": {"file_path": str(ws.root / "src" / "a.py"), "content": "x"},
                         "cwd": str(ws.root)}).allow
