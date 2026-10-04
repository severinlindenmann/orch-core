import json
import os
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
STORE_ROOT = PLUGIN_ROOT.parents[1]
DESCRIPTION = "Let coding agents do the work through local Markdown tickets, with a human gate at every step."


def test_plugin_manifest_matches_package_version():
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    version = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert manifest["name"] == "orch-core"
    assert manifest["version"] == version
    assert manifest["description"] == DESCRIPTION


def test_hooks_call_the_bundled_wrapper():
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "Bash|Edit|Write|MultiEdit|Read|Grep|Glob|NotebookEdit"
    assert pre["hooks"][0]["command"] == '"${CLAUDE_PLUGIN_ROOT}/bin/orch" guard --hook-json'
    assert hooks["SessionStart"][0]["hooks"][0]["command"] == '"${CLAUDE_PLUGIN_ROOT}/bin/orch" hook session-start'


def test_wrapper_is_executable_posix_script():
    script = PLUGIN_ROOT / "bin" / "orch"
    raw = script.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n")
    assert b"\r" not in raw  # CRLF breaks /bin/sh on Windows checkouts (see .gitattributes)
    if sys.platform != "win32":
        assert script.stat().st_mode & stat.S_IXUSR


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_wrapper_fails_open_without_uv(tmp_path):
    r = subprocess.run([shutil.which("sh"), str(PLUGIN_ROOT / "bin" / "orch"), "guard"],
                       env={"PATH": str(tmp_path)}, capture_output=True, text=True, input="{}")
    assert r.returncode == 127 and "uv is not installed" in r.stderr  # not 2: never blocks a tool call


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("uv") is None, reason="needs sh and uv")
def test_wrapper_runs_bundled_cli():
    from orch import __version__
    r = subprocess.run([shutil.which("sh"), str(PLUGIN_ROOT / "bin" / "orch"), "version"],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, env={**os.environ}, timeout=300)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == __version__


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("uv") is None, reason="needs sh and uv")
def test_wrapper_picks_up_source_changes(tmp_path):
    copy_dir = tmp_path / "copy dir"
    shutil.copytree(PLUGIN_ROOT, copy_dir, ignore=shutil.ignore_patterns(".venv"))
    script = str(copy_dir / "bin" / "orch")

    r1 = subprocess.run([shutil.which("sh"), script, "version"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL,
                        env={**os.environ}, timeout=300)
    assert r1.returncode == 0, r1.stderr

    init_py = copy_dir / "src" / "orch" / "__init__.py"
    init_py.write_text('__version__ = "9.9.9-test"\n', encoding="utf-8")

    r2 = subprocess.run([shutil.which("sh"), script, "version"],
                        capture_output=True, text=True, stdin=subprocess.DEVNULL,
                        env={**os.environ}, timeout=300)
    assert r2.returncode == 0, r2.stderr
    assert r2.stdout.strip() == "9.9.9-test"


@pytest.mark.skipif(not (STORE_ROOT / ".claude-plugin" / "marketplace.json").exists(), reason="not inside the marketplace repo")
def test_marketplace_entry():
    market = json.loads((STORE_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    entry = next(p for p in market["plugins"] if p["name"] == "orch-core")
    assert entry == {"name": "orch-core", "source": "./plugins/orch-core",
                     "description": DESCRIPTION, "category": "productivity"}


def test_gitattributes_pins_lf_for_scripts():
    text = (PLUGIN_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "bin/* text eol=lf" in text and "*.sh text eol=lf" in text


# -- the wrapper never exits 2 (that would block every tool call) ------------------------

_NEEDS_UV = pytest.mark.skipif(shutil.which("sh") is None or shutil.which("uv") is None, reason="needs sh and uv")
_COPY_IGNORE = shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "*.egg-info")


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "UV_PROJECT_ENVIRONMENT", "VIRTUAL_ENV")}
    env.update(extra)
    return env


def _wrapper(script, *args, payload=None, env=None):
    return subprocess.run([shutil.which("sh"), str(script), *args], capture_output=True, text=True,
                          input=json.dumps(payload) if payload is not None else "",
                          env=env or _env(), timeout=300)


@pytest.fixture
def readonly_copy(tmp_path):
    if sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        pytest.skip("needs POSIX permissions and a non-root user")
    copy_dir = tmp_path / "ro plugin"
    shutil.copytree(PLUGIN_ROOT, copy_dir, ignore=_COPY_IGNORE)
    subprocess.run(["chmod", "-R", "a-w", str(copy_dir)], check=True)
    yield copy_dir
    subprocess.run(["chmod", "-R", "u+w", str(copy_dir)], check=True)


@_NEEDS_UV
def test_wrapper_never_exits_2_on_a_read_only_plugin_root(readonly_copy):
    r = _wrapper(readonly_copy / "bin" / "orch", "guard", "--hook-json",
                 payload={"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert not (readonly_copy / ".venv").exists()
    assert r.returncode != 2, r.stderr
    assert r.returncode == 1, (r.returncode, r.stderr)


@_NEEDS_UV
def test_wrapper_puts_the_venv_into_plugin_data(tmp_path, readonly_copy):
    data = tmp_path / "plugin data"
    r = _wrapper(readonly_copy / "bin" / "orch", "version", env=_env(CLAUDE_PLUGIN_DATA=str(data)))
    assert r.returncode == 0, r.stderr
    assert (data / "venv").is_dir()
    assert not (readonly_copy / ".venv").exists()


@_NEEDS_UV
def test_wrapper_maps_cli_errors_to_exit_1(tmp_path):
    r = _wrapper(PLUGIN_ROOT / "bin" / "orch", "no-such-command")
    assert r.returncode == 1, (r.returncode, r.stderr)


@_NEEDS_UV
def test_wrapper_keeps_other_exit_codes(tmp_path):
    (tmp_path / "orchestrator").mkdir()
    (tmp_path / "orchestrator" / "config.json").write_text("{", encoding="utf-8")
    r = subprocess.run([shutil.which("sh"), str(PLUGIN_ROOT / "bin" / "orch"), "check"], capture_output=True,
                       text=True, input="", cwd=tmp_path, env=_env(), timeout=300)
    assert r.returncode == 5, (r.returncode, r.stderr)


def _ticket_workspace(tmp_path):
    from orch.core.ops import Ops
    from orch.core.events import Actor
    from orch.core.workspace import Workspace
    root = tmp_path / "hook ws"
    (root / "orchestrator").mkdir(parents=True)
    (root / "orchestrator" / "config.json").write_text(
        json.dumps({"schema": 1, "customer": "acme", "id": {"prefix": "L", "pad": 4}}), encoding="utf-8")
    ws = Workspace.open(root)
    t = Ops(ws, Actor("human", "you", "tty")).new("Hook test", type="feature", priority="normal", size="s")
    path = next(ws.tickets_dir.rglob(f"{t.id}-*.md"))
    return root, path


@_NEEDS_UV
def test_plugin_guard_denies_ticket_status_edit_end_to_end(tmp_path):
    root, path = _ticket_workspace(tmp_path)
    status = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("status:"))
    payload = {"tool_name": "Edit", "cwd": str(root),
               "tool_input": {"file_path": str(path), "old_string": status, "new_string": "status: done"}}
    r = _wrapper(PLUGIN_ROOT / "bin" / "orch", "guard", "--hook-json", payload=payload)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "status" in out["permissionDecisionReason"] and "orch" in out["permissionDecisionReason"]


@_NEEDS_UV
@pytest.mark.parametrize("cmd", ["orch serve", '"${CLAUDE_PLUGIN_ROOT}/bin/orch" serve --no-open'])
def test_plugin_guard_denies_agent_serve_end_to_end(tmp_path, cmd):
    root, _ = _ticket_workspace(tmp_path)
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": cmd}}
    r = _wrapper(PLUGIN_ROOT / "bin" / "orch", "guard", "--hook-json", payload=payload)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "dashboard is the human's" in out["permissionDecisionReason"]


@_NEEDS_UV
def test_plugin_guard_allow_prints_nothing_end_to_end(tmp_path):
    root, _ = _ticket_workspace(tmp_path)
    payload = {"tool_name": "Bash", "cwd": str(root), "tool_input": {"command": "orch list"}}
    r = _wrapper(PLUGIN_ROOT / "bin" / "orch", "guard", "--hook-json", payload=payload)
    assert r.returncode == 0 and r.stdout == "", (r.stdout, r.stderr)
