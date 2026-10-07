import json
import shutil
import subprocess

import pytest

from orch.cli import run
from orch.hooks.install import hook_state, install_hooks
from orch.onboarding import doctor

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def codes(checks):
    return {c.code: c.ok for c in checks}


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    init_repo(path)
@needs_git
def test_doctor_outside_workspace(tmp_path):
    _git_init(tmp_path / "repo")
    got = codes(doctor(tmp_path / "repo"))
    assert got["git"] is True and got["workspace"] is False
    assert "config" not in got and "hooks" not in got


def test_doctor_outside_git(tmp_path):
    got = codes(doctor(tmp_path))
    assert got["git"] is False and got["workspace"] is False


@needs_git
def test_doctor_reports_adopt_repos_and_hooks(configure, ws_root):
    _git_init(ws_root / "hub")
    ws = configure(git={"repos": {"hub": {}}})
    (ws_root / "CLAUDE.md").write_text("# mine\n", encoding="utf-8")
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["config"].ok is True
    assert checks["adopt"].ok is False and "CLAUDE.md" in checks["adopt"].message
    assert checks["adopt"].fix == "orch instructions sync --adopt"
    assert checks["hooks"].ok is False and checks["hooks"].fix == "orch hooks install"
    install_hooks(ws)
    assert hook_state(ws_root / "hub") == "installed"
    assert {c.code: c.ok for c in doctor(ws_root)}["hooks"] is True


def test_doctor_without_repos(ws_root, ws):
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["repos"].ok is False and "git.repos" in checks["repos"].message


def test_doctor_invalid_config(configure, ws_root):
    configure(dashboard={"port": "abc"})
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["config"].ok is False and "dashboard/port" in checks["config"].message


def test_doctor_plugin_mode(configure, ws_root):
    configure(harnesses=["claude-plugin"])
    assert {c.code: c.ok for c in doctor(ws_root)}["plugin"] is False
    assert run(["instructions", "sync"]) == 0
    assert {c.code: c.ok for c in doctor(ws_root)}["plugin"] is True


def test_terminal_cli_ignores_plugin_wrapper(ws_root, ws, tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    (plugin / "bin").mkdir(parents=True)
    fake = plugin / "bin" / "orch"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    monkeypatch.setattr(shutil, "which", lambda name, path=None: str(fake) if name == "orch" else "/usr/bin/uv")
    check = next(c for c in doctor(ws_root) if c.code == "terminal-cli")
    assert check.ok is False and "uv tool install" in check.fix and str(plugin) in check.fix


def test_cli_doctor_json(ws_root, ws, capsys):
    assert run(["doctor", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {"code", "ok", "message", "fix"} <= set(rows[0])


@needs_git
def test_doctor_skill_copies_not_applicable_outside_plugin_mode(ws_root, ws):
    got = codes(doctor(ws_root))
    assert "skill-copies" not in got


@needs_git
def test_doctor_skill_copies_ok_when_absent(configure, ws_root):
    configure(harnesses=["claude-plugin"])
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["skill-copies"].ok is True


@needs_git
def test_doctor_skill_copies_not_ok_when_present(configure, ws_root):
    configure(harnesses=["claude-plugin"])
    leftover = ws_root / ".claude" / "skills" / "orch-tickets"
    leftover.mkdir(parents=True)
    checks = {c.code: c for c in doctor(ws_root)}
    assert checks["skill-copies"].ok is False
    assert "orch-tickets" in checks["skill-copies"].message
    assert checks["skill-copies"].fix is not None and "delete" in checks["skill-copies"].fix.lower()


# -- final fix wave: terminal-cli, skill copies, harness ---------------------------------

from orch import onboarding  # noqa: E402
from orch.onboarding import OPEN_ITEM_CODES, _package_plugin_root as REAL_PACKAGE_ROOT  # noqa: E402
from conftest import init_repo

PLUGIN_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
in_plugin = pytest.mark.skipif(not (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").exists(),
                               reason="not a plugin checkout")
posix = pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX executables")


def _exe(path, body=None):
    from orch import __version__
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + (f"echo {__version__}\n" if body is None else body), encoding="utf-8")
    path.chmod(0o755)
    return path


def _terminal(ws_root):
    return next(c for c in doctor(ws_root) if c.code == "terminal-cli")


@in_plugin
def test_plugin_root_is_derived_from_the_package():
    assert REAL_PACKAGE_ROOT() == PLUGIN_ROOT


@posix
def test_terminal_cli_skips_the_plugins_own_venv(ws_root, ws, tmp_path, monkeypatch):
    venv = tmp_path / "plugin-data" / "venv"
    _exe(venv / "bin" / "orch")
    real = _exe(tmp_path / "home" / ".local" / "bin" / "orch")
    monkeypatch.setattr(onboarding.sys, "prefix", str(venv))
    monkeypatch.setenv("PATH", f"{venv / 'bin'}{__import__('os').pathsep}{real.parent}")
    check = _terminal(ws_root)
    assert check.ok is True and str(real.parent) in check.message


@posix
def test_terminal_cli_accepts_uv_tool_symlink_into_running_venv(ws_root, ws, tmp_path, monkeypatch):
    tool_venv = tmp_path / "uv-tools" / "orch-core"
    target = _exe(tool_venv / "bin" / "orch")
    link_dir = tmp_path / "home" / ".local" / "bin"
    link_dir.mkdir(parents=True)
    (link_dir / "orch").symlink_to(target)
    monkeypatch.setattr(onboarding.sys, "prefix", str(tool_venv))
    monkeypatch.setenv("PATH", str(link_dir))
    check = _terminal(ws_root)
    assert check.ok is True


@posix
@in_plugin
def test_terminal_cli_skips_orch_under_the_derived_plugin_root(ws_root, ws, tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, "_package_plugin_root", REAL_PACKAGE_ROOT)
    monkeypatch.setattr(onboarding.sys, "prefix", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("PATH", str(PLUGIN_ROOT / "bin"))
    check = _terminal(ws_root)
    assert check.ok is False
    assert check.fix == f'uv tool install "{PLUGIN_ROOT}[dashboard]"'


def test_terminal_cli_fix_placeholder_when_root_unknown(ws_root, ws, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    check = _terminal(ws_root)
    assert check.ok is False and "<path to the orch-core plugin folder>" in check.fix


HARNESS_FIX = ('in orchestrator/config.json set "harnesses" to use "claude-plugin" instead of "claude" '
               '(e.g. "harnesses": ["claude-plugin"]), then run `orch instructions sync`, '
               'then delete the leftover .claude/skills copies')


def test_harness_is_an_open_item_code():
    assert "harness" in OPEN_ITEM_CODES


def test_doctor_harness_ok_without_plugin(ws_root, ws):
    check = {c.code: c for c in doctor(ws_root)}["harness"]
    assert check.ok is True


def test_doctor_harness_flags_claude_mode_under_plugin_env(ws_root, ws, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    check = {c.code: c for c in doctor(ws_root)}["harness"]
    assert check.ok is False and "run twice" in check.message and check.fix == HARNESS_FIX


@in_plugin
def test_doctor_harness_flags_claude_mode_via_derived_root(ws_root, ws, monkeypatch):
    monkeypatch.setattr(onboarding, "_package_plugin_root", REAL_PACKAGE_ROOT)
    assert {c.code: c.ok for c in doctor(ws_root)}["harness"] is False


@pytest.mark.parametrize("where", ["project", "user"])
def test_doctor_harness_flags_claude_mode_via_settings(ws_root, ws, monkeypatch, tmp_path, where):
    from orch.instructions.settings import PLUGIN_ID
    if where == "project":
        path = ws_root / ".claude" / "settings.json"
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        path = tmp_path / "cc" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabledPlugins": {PLUGIN_ID: True}}), encoding="utf-8")
    assert {c.code: c.ok for c in doctor(ws_root)}["harness"] is False


def test_doctor_harness_not_checked_in_plugin_mode(configure, ws_root, monkeypatch, tmp_path):
    configure(harnesses=["claude-plugin"])
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "plugin"))
    assert "harness" not in codes(doctor(ws_root))


@needs_git
def test_doctor_reuses_known_hook_states(configure, ws_root, monkeypatch):
    import orch.hooks.install as install_mod
    _git_init(ws_root / "hub")
    configure(git={"repos": {"hub": {}}})
    monkeypatch.setattr(install_mod, "hook_state", lambda repo: (_ for _ in ()).throw(AssertionError("asked git again")))
    got = {c.code: c.ok for c in doctor(ws_root, hook_states={(ws_root / "hub").resolve(): "installed"})}
    assert got["hooks"] is True


# -- #162: CLI/plugin version skew, legacy plugin ids ---------------------------------------

def _plugin(tmp_path, version):
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "orch-core", "version": version}))
    return root


def _terminal_with(ws_root, tmp_path, monkeypatch, body, plugin_version="0.4.1"):
    root = _plugin(tmp_path, plugin_version)
    cli = _exe(tmp_path / "home" / ".local" / "bin" / "orch", body)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setattr(onboarding.sys, "prefix", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("PATH", str(cli.parent))
    return root, _terminal(ws_root)


@posix
def test_terminal_cli_ok_when_versions_match(ws_root, ws, tmp_path, monkeypatch):
    _, check = _terminal_with(ws_root, tmp_path, monkeypatch, "echo 0.4.1\n")
    assert check.ok is True and "orch 0.4.1" in check.message


@posix
def test_terminal_cli_flags_an_older_cli_and_offers_the_upgrade(ws_root, ws, tmp_path, monkeypatch):
    root, check = _terminal_with(ws_root, tmp_path, monkeypatch, "echo 0.3.0\n")
    assert check.ok is False
    assert "orch 0.3.0" in check.message and "0.4.1" in check.message
    assert check.fix == f'uv tool install --force "{root}[dashboard]"'


@posix
def test_terminal_cli_flags_a_cli_without_version_flag(ws_root, ws, tmp_path, monkeypatch):
    body = "echo \"Error: No such option: --version\" >&2\nexit 2\n"
    root, check = _terminal_with(ws_root, tmp_path, monkeypatch, body)
    assert check.ok is False and "too old to answer `orch --version`" in check.message
    assert "--force" in check.fix and str(root) in check.fix


@posix
def test_terminal_cli_version_is_cached_until_the_file_changes(ws_root, ws, tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    body = f"echo x >> {calls}\necho 0.4.1\n"
    _terminal_with(ws_root, tmp_path, monkeypatch, body)
    _terminal(ws_root)
    assert calls.read_text().count("x") == 1


@posix
def test_open_setup_items_does_not_run_the_terminal_cli(ws_root, ws, tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    root = _plugin(tmp_path, "0.4.1")
    cli = _exe(tmp_path / "bin" / "orch", f"echo x >> {calls}\necho 0.4.1\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("PATH", str(cli.parent))
    onboarding.open_setup_items(ws)
    assert not calls.exists()


def test_legacy_plugin_is_an_open_item_code():
    assert "legacy-plugin" in OPEN_ITEM_CODES


def _project_settings(ws_root, data, name="settings.json"):
    path = ws_root / ".claude" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _legacy(ws_root):
    return next(c for c in doctor(ws_root) if c.code == "legacy-plugin")


def test_doctor_legacy_plugin_ok_without_one(ws_root, ws):
    assert _legacy(ws_root).ok is True


def test_doctor_flags_legacy_plugin_and_sync_removes_only_it(configure, ws_root):
    configure(harnesses=["claude-plugin"])
    path = _project_settings(ws_root, {"enabledPlugins": {
        "orch-ticket-workflow@ai-convenience-store": True, "orch-core@orch-core": True, "other@market": True}})
    check = _legacy(ws_root)
    assert check.ok is False
    assert "orch-ticket-workflow@ai-convenience-store" in check.message and ".claude/settings.json" in check.message
    assert "orch instructions sync" in check.fix
    assert run(["instructions", "sync"]) == 0
    assert json.loads(path.read_text())["enabledPlugins"] == {"orch-core@orch-core": True, "other@market": True}
    assert _legacy(ws_root).ok is True


def test_doctor_legacy_plugin_in_local_settings_is_fixed_by_hand(configure, ws_root):
    configure(harnesses=["claude-plugin"])
    _project_settings(ws_root, {"enabledPlugins": {"orch-ticket-workflow@x": True}}, "settings.local.json")
    check = _legacy(ws_root)
    assert check.ok is False
    assert check.fix == "remove orch-ticket-workflow@x from enabledPlugins in .claude/settings.local.json"


def test_doctor_ignores_a_disabled_legacy_plugin(ws_root, ws):
    _project_settings(ws_root, {"enabledPlugins": {"orch-ticket-workflow@ai-convenience-store": False}})
    assert _legacy(ws_root).ok is True
