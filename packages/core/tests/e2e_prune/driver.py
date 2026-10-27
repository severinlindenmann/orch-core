#!/usr/bin/env python3.14
"""Driver of the real `orch host prune` e2e (run it through run.sh: it starts here with a cleared environment).

Everything happens under HOME (a throw-away folder). A real install, like the owner's: `uv tool install` of orch-core
with the dashboard add-on, hub/relay/publish through `orch install` (pidfile runner: no launchd label of this Mac is
touched), a demo workspace made by the real `orch setup` (played over the setup socket by the dashboard kit), a host
started in the foreground. Then prune: --dry-run changes nothing, --yes --workspaces keep leaves no trace of orch in
HOME and keeps the workspace's own files."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "packages/addons/dashboard/server/tests"))
import realhost_kit as K  # noqa: E402

HOME = Path(os.environ["HOME"])
TMP = Path(os.environ["PRUNE_E2E_TMP"])
BIN = HOME / ".local" / "bin"
TOOL_PY = HOME / ".local/share/uv/tools/orch-core/bin/python"
PORTS = {"publish": 18102, "relay": 18101, "hub": 18100}
USER_FILES = {
    "notes.txt": "my own notes\n",
    "src/main.py": "print('hello')\n",
    "README.md": "# demo\n",
    ".gitignore": "node_modules/\n",
    "AGENTS.md": "# My project rules\n\nBe kind.\n",
}


def log(msg: str) -> None:
    print(f"[e2e-prune] {msg}", flush=True)


def clean_env(**extra: str) -> dict[str, str]:
    env = {
        "HOME": str(HOME),
        "PATH": f"{BIN}:{os.environ['PATH']}",
        "USER": os.environ.get("USER", "tester"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": "xterm",
        "PYTHONWARNINGS": "ignore",
        "ORCH_INSTALL_SERVICE": "pidfile",
        "ORCH_DASHBOARD_SKIP_UI": "1",  # the add-on's server without its web page: this test never opens a browser
    }
    env.update(extra)
    return env


def guard() -> None:
    bad = [k for k in os.environ if k.startswith("ORCH_") or k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")]
    assert not bad, f"the environment was not cleared (env -i): {bad}"
    real = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert HOME != real and real not in HOME.parents or ".tprune-" in str(HOME), f"HOME {HOME} looks real"
    assert ".tprune-" in str(TMP) and HOME.parent == TMP, "HOME must be the throw-away folder"


def run(argv: list[str], *, ok: bool = True, timeout: int = 900, **env: str) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(argv))
    done = subprocess.run(
        argv, env=clean_env(**env), capture_output=True, text=True, timeout=timeout, cwd=HOME, check=False
    )
    if ok and done.returncode != 0:
        raise SystemExit(f"FAILED ({done.returncode}): {' '.join(argv)}\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}")
    return done


class W(K.Workspace):
    """The dashboard kit's workspace with a clean (non-dev) environment and the installed tool's Python."""

    def env(self, **extra: str) -> dict[str, str]:
        return clean_env(**extra)

    def orch(self, *args: str) -> list[str]:
        return [str(TOOL_PY), "-W", "ignore", "-m", "orch", *args]

    def stop_dashboard(self, wait: float = 15.0) -> None:  # prune stops it
        return None


def snapshot(root: Path) -> list[str]:
    out = []
    for dp, dn, fn in os.walk(root):
        out += [os.path.join(dp, n) for n in dn + fn]
    return sorted(out)


def traces(root: Path) -> list[str]:
    """Anything below HOME whose name says orch, except uv's own folders and the demo workspace's files."""
    skip = (HOME / ".local" / "share" / "uv", HOME / ".cache")
    found = []
    for dp, dn, fn in os.walk(root):
        here = Path(dp)
        dn[:] = [d for d in dn if here / d not in skip]
        found += [os.path.join(dp, n) for n in dn + fn if "orch" in n.lower()]
    return found


def main() -> int:
    guard()
    t0 = time.time()
    state = HOME / ".config" / "orch"
    ws_root = HOME / "work" / "proj"

    # -- 1. install, as the owner does
    server = REPO / "packages/addons/dashboard/server"
    run(["uv", "tool", "install", "--python", "3.14", "--with", f"orch-dashboard @ {server.as_uri()}",
         str(REPO / "packages/core")])  # fmt: skip
    assert TOOL_PY.exists() and (BIN / "orch").exists()
    for part in ("publish", "relay", "hub"):
        run([str(BIN / "orch"), "install", part, "--source", str(REPO / "packages" / part),
             "--port", str(PORTS[part]), "--address", "127.0.0.1", "--yes"])  # fmt: skip

    # -- 2. a demo workspace with the user's own files and git history, then the real setup
    ws_root.mkdir(parents=True)
    for rel, text in USER_FILES.items():
        (ws_root / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws_root / rel).write_text(text)
    git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-C", str(ws_root)]
    run(["git", "init", "-q", "--initial-branch=main", str(ws_root)])
    run([*git, "add", "-A"])
    run([*git, "commit", "-q", "-m", "my first commit"])
    ws = W(TMP, prefix="DEMO", dirname="proj", owner="Demo Owner")
    ws.home, ws.state, ws.run, ws.root = HOME, state, state / "run", ws_root
    ws.start_host()
    try:
        ws.init()
        log(f"workspace {ws.ws_id} made by orch setup")
        pids = pid_list(state)
        log(f"recorded service pids: {pids}")
        status = run([str(BIN / "orch"), "install", "status"]).stdout
        log("install status:\n" + status)
        assert "hub" in status and "running" in status
        before = snapshot(HOME)
        assert any("orch" in p for p in before)

        # -- 3. dry run changes nothing
        dry = run([str(BIN / "orch"), "host", "prune", "--dry-run"])
        log("dry-run output:\n" + dry.stdout)
        assert snapshot(HOME) == before, "--dry-run changed something"
        for needle in ("Installed parts", "Keys", "Workspace " + str(ws_root), "Not removable by prune"):
            assert needle in dry.stdout, f"the inventory lacks {needle!r}"

        # -- 4. prune for real: --yes never touches the workspace folder; the tool is kept for now to run it twice
        prune = [str(BIN / "orch"), "host", "prune", "--yes", "--keep-tool"]
        done = run([*prune, "--workspaces", "keep"], ok=False)
        log(f"prune exit {done.returncode}\n{done.stdout}\n{done.stderr}")
        assert done.returncode == 0, "prune failed"
    finally:
        ws.stop_host()  # the foreground host is not a service: prune reports it, the owner of the process stops it
    time.sleep(1.0)

    # -- 5. nothing of orch is left except the command itself
    left = [p for p in traces(HOME) if not p.startswith(str(BIN))]
    assert not left, "traces of orch left under HOME:\n" + "\n".join(left)
    assert not state.exists(), f"{state} is still there: {os.listdir(state)}"
    alive = [p for p in pids if alive_pid(p)]
    assert not alive, f"service processes still running: {alive}"
    for rel, text in USER_FILES.items():
        assert (ws_root / rel).read_text() == text, f"{rel} was changed"
    for gone in (".orch", ".orch-work", ".agents", ".claude"):
        assert not (ws_root / gone).exists(), f"{gone} is still in the workspace"
    claude = ws_root / "CLAUDE.md"
    assert not claude.exists() or claude.read_text() == "@AGENTS.md\n"
    log_out = run([*git, "log", "--format=%s"]).stdout.splitlines()
    assert "my first commit" in log_out, f"git history lost: {log_out}"

    # -- 6. a second run finds nothing; a run without --keep-tool takes the command away too
    again = run(prune)
    assert "Nothing to remove" in again.stdout, again.stdout
    last = run([str(BIN / "orch"), "host", "prune", "--yes"])
    log("last run:\n" + last.stdout)
    assert not (BIN / "orch").exists(), "the orch command is still installed"
    assert not traces(HOME), "traces of orch left under HOME:\n" + "\n".join(traces(HOME))
    log(f"OK in {time.time() - t0:.0f}s")
    return 0


def pid_list(state: Path) -> list[int]:
    try:
        doc = json.loads((state / "install" / "install.json").read_text())
    except (OSError, ValueError):
        return []
    pids = [p.get("pid") for p in doc.get("parts", {}).values()] + [doc.get("dashboard", {}).get("pid")]
    return [p for p in pids if isinstance(p, int)]


def alive_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    sys.exit(main())
