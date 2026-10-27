#!/usr/bin/env python3.14
"""Driver of the real `orch stop` / `orch start` / `orch services` e2e (run it through run.sh: cleared environment).

Everything happens under HOME (a throw-away folder). A real install like the owner's: `uv tool install` of orch-core
with the dashboard add-on, hub/relay/publish as background services of the person's service manager (on a Mac: launchd,
with labels derived from this throw-away state folder), two demo workspaces made by the real `orch setup`, a host
started in the foreground (a host started by hand cannot be stopped by orch: the e2e shows that too)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "packages/addons/dashboard/server/tests"))
import realhost_kit as K  # noqa: E402

HOME = Path(os.environ["HOME"])
TMP = Path(os.environ["STOP_E2E_TMP"])
BIN = HOME / ".local" / "bin"
TOOL_PY = HOME / ".local/share/uv/tools/orch-core/bin/python"
PORTS = {"publish": 18102, "relay": 18101, "hub": 18100}
SERVICE = "launchd" if sys.platform == "darwin" else "auto"


def log(msg: str) -> None:
    print(f"[e2e-stop] {msg}", flush=True)


def clean_env(**extra: str) -> dict[str, str]:
    env = {
        "HOME": str(HOME),
        "PATH": f"{BIN}:{os.environ['PATH']}",
        "USER": os.environ.get("USER", "tester"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "TERM": "xterm",
        "PYTHONWARNINGS": "ignore",
        "ORCH_INSTALL_SERVICE": SERVICE,
        "ORCH_DASHBOARD_SKIP_UI": "1",
    }
    env.update(extra)
    return env


def guard() -> None:
    bad = [k for k in os.environ if k.startswith("ORCH_") or k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME")]
    assert not bad, f"the environment was not cleared (env -i): {bad}"
    assert ".tstop-" in str(TMP) and HOME.parent == TMP, "HOME must be the throw-away folder"


def run(argv, *, ok=True, cwd=None, timeout=900) -> subprocess.CompletedProcess[str]:
    log("$ " + " ".join(argv))
    done = subprocess.run(
        argv, env=clean_env(), capture_output=True, text=True, timeout=timeout, cwd=cwd or HOME, check=False,
        start_new_session=True,
    )  # fmt: skip
    if ok and done.returncode != 0:
        raise SystemExit(f"FAILED ({done.returncode}): {' '.join(argv)}\n{done.stdout[-1500:]}\n{done.stderr[-1500:]}")
    return done


def orch(*args: str, cwd=None, ok=True) -> subprocess.CompletedProcess[str]:
    done = run([str(BIN / "orch"), *args], cwd=cwd, ok=ok)
    log(f"-> exit {done.returncode}\n{done.stdout}{done.stderr}")
    return done


class W(K.Workspace):
    def env(self, **extra: str) -> dict[str, str]:
        return clean_env(**extra)

    def orch(self, *args: str) -> list[str]:
        return [str(TOOL_PY), "-W", "ignore", "-m", "orch", *args]

    def stop_dashboard(self, wait: float = 15.0) -> None:
        return None

    def make_dirs(self) -> None:
        for path in (self.base, self.state, self.home, self.home / ".config", self.root):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def wait_for(cond, what: str, seconds: float = 40.0) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return
        time.sleep(0.5)
    raise SystemExit(f"timeout waiting for: {what}")


def rows(text: str) -> dict[str, str]:
    """``service name -> state`` from the `orch services` table."""
    out = {}
    for line in text.splitlines():
        for name in (
            "the host service",
            "the agent runner",
            "the records timer",
            "the dashboard",
            "the hub",
            "the relay",
            "publish",
        ):
            if line.startswith(name):
                out[name] = line[22:36].strip()
    return out


def make_workspace(state: Path, prefix: str, dirname: str) -> W:
    root = HOME / "work" / dirname
    root.mkdir(parents=True)
    (root / "notes.txt").write_text("mine\n")
    git = ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-C", str(root)]
    run(["git", "init", "-q", "--initial-branch=main", str(root)])
    run([*git, "add", "-A"])
    run([*git, "commit", "-q", "-m", "first"])
    ws = W(TMP, prefix=prefix, dirname=dirname, owner="Demo Owner")
    ws.home, ws.state, ws.run, ws.root = HOME, state, state / "run", root
    return ws


def main() -> int:
    guard()
    t0 = time.time()
    state = HOME / ".config" / "orch"
    server = REPO / "packages/addons/dashboard/server"
    pkg = f"orch-dashboard @ {server.as_uri()}"
    run(["uv", "tool", "install", "--python", "3.14", "--with", pkg, str(REPO / "packages/core")])
    for part in ("publish", "relay", "hub"):
        src = str(REPO / "packages" / part)
        orch("install", part, "--source", src, "--port", str(PORTS[part]), "--address", "127.0.0.1", "--yes")

    ws1 = make_workspace(state, "ONE", "proj1")
    ws2 = make_workspace(state, "TWO", "proj2")
    ws1.start_host()
    try:
        ws1.init()
        ws2.init()
        doc = json.loads((state / "install" / "install.json").read_text())
        dash_port = doc["dashboard"]["port"]
        up = lambda: all(listening(p) for p in (*PORTS.values(), dash_port))  # noqa: E731
        wait_for(up, "hub, relay, publish and the dashboard listening")
        plists = sorted((HOME / "Library" / "LaunchAgents").glob("*.plist")) if sys.platform == "darwin" else []

        # -- 1. the table: everything runs
        t = orch("services").stdout
        assert (
            rows(t)["the hub"] == rows(t)["the relay"] == rows(t)["publish"] == rows(t)["the dashboard"] == "running"
        ), t
        assert "workspaces on this computer (2)" in t, t
        assert "NetBird" not in t

        # -- 2. outside a workspace: explains, stops nothing
        out = orch("stop", cwd=HOME, ok=False)
        assert out.returncode == 1 and "orch stop all" in out.stderr and up()

        # -- 3. `orch stop` in a folder: this workspace's agents only, the others (and the shared services) stay up
        out = orch("stop", cwd=ws1.root)
        assert out.returncode == 0 and "shared with 1 other workspace" in out.stdout and up(), out.stdout

        # -- 4. dry run changes nothing
        orch("stop", "all", "--dry-run")
        assert up()

        # -- 5. stop all: parts and dashboard stop; the host started by hand is named, not killed
        out = orch("stop", "all", ok=False)
        assert out.returncode == 1 and "started by hand" in out.stdout, out.stdout
        wait_for(lambda: not any(listening(p) for p in (*PORTS.values(), dash_port)), "nothing listening")
        if sys.platform == "darwin":
            disabled = run(["launchctl", "print-disabled", f"gui/{os.getuid()}"]).stdout
            for plist in plists:
                assert plist.exists(), "stop removed a unit"
                assert f'"{plist.stem}" => disabled' in disabled or f'"{plist.stem}" => true' in disabled, plist.stem
        t = orch("services").stdout
        assert {rows(t)[k] for k in ("the hub", "the relay", "publish", "the dashboard")} == {"stopped"}, t
        assert "stopped by orch stop" in t

        # -- 6. the person ends the host they started; then there is nothing left to stop
        ws1.stop_host()
        again = orch("stop", "all")
        assert again.returncode == 0 and "Nothing to do" in again.stdout, again.stdout

        # -- 7. start all: everything comes back, on the same ports
        out = orch("start", "all")
        assert out.returncode == 0, out.stdout
        wait_for(up, "everything listening again after start")
        t = orch("services").stdout
        assert {rows(t)[k] for k in ("the hub", "the relay", "publish", "the dashboard")} == {"running"}, t
        assert "stopped by orch stop" not in t

        # -- 8. an agent is refused
        env_agent = {**clean_env(), "ORCH_SESSION": "s_x"}
        refused = subprocess.run(
            [str(BIN / "orch"), "stop", "all"], env=env_agent, capture_output=True, text=True, cwd=HOME
        )
        assert refused.returncode == 3 and "never does it" in refused.stderr
    finally:
        ws1.stop_host()

    # -- 9. clean up: prune removes the units; the launchd disable overrides we made are cleared too
    labels = [p.stem for p in plists] if sys.platform == "darwin" else []
    orch("host", "prune", "--yes", "--keep-tool")
    for label in labels:
        subprocess.run(["launchctl", "enable", f"gui/{os.getuid()}/{label}"], capture_output=True)
    orch("host", "prune", "--yes")
    log(f"OK in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
