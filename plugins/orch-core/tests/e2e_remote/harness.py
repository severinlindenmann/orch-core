"""Building blocks of the Orch Remote end-to-end run: a local TIX server, desk devices with the real relay tool, two
workspaces whose dashboards run with the remote flag, and the owner's side of the Remote tab.

Nothing here talks to the internet. TIX runs from a checkout of orch-tix (ORCH_TIX_DIR) on a free loopback port,
under the name `localhost` (a WebAuthn relying party cannot be an IP address)."""
from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from html import unescape
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "serve_launcher.py"
SNAPS = Path(os.environ.get("ORCH_E2E_SNAPS", "/tmp/orch-e2e-remote-snaps"))  # pictures of a failure
SNAPS.mkdir(parents=True, exist_ok=True)
_Popen = subprocess.Popen  # the suite's autouse fixtures may replace Popen on the module; keep the real one
AGENT_VARS = ("ORCH_HOME", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ORCH_HARNESS", "ORCH_SESSION", "ORCH_MODEL",
              "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "GEMINI_CLI",
              "CLAUDE_CONFIG_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")


def find_tix() -> Path | None:
    """ORCH_TIX_DIR, else an `orch-tix` folder next to this checkout (or next to any folder above it)."""
    env = os.environ.get("ORCH_TIX_DIR")
    if env:
        return Path(env).expanduser().resolve()
    for parent in HERE.parents:
        cand = parent.parent / "orch-tix"
        if (cand / "fileshare").is_dir():
            return cand
    return None


def tix_problem(tix: Path | None) -> str | None:
    if tix is None or not tix.is_dir():
        return "no orch-tix checkout: set ORCH_TIX_DIR to one (a folder with fileshare/ and skill/sharing/)"
    for rel in ("fileshare/static/remote.html", "skill/sharing/sharing.py", "addons/orch-tix/orch-addon.json",
                "tests/helpers/browser_sim.py"):
        if not (tix / rel).is_file():
            return f"the orch-tix checkout at {tix} lacks {rel}: it is too old or incomplete for the Remote pages"
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in AGENT_VARS and not k.startswith("CMUX_")}
    env.update({"NO_PROXY": "127.0.0.1,localhost", "PYTHONUNBUFFERED": "1", **extra})
    return env


def until(pred, timeout: float = 30.0, every: float = 0.2, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = pred()
            if last:
                return last
        except (AssertionError, httpx.HTTPError, OSError) as e:
            last = e
        time.sleep(every)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what} (last: {last!r})")


@contextmanager
def state_env(path: Path):
    """ORCH_STATE_DIR for in-process calls that write the addon registry."""
    old = os.environ.get("ORCH_STATE_DIR")
    os.environ["ORCH_STATE_DIR"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("ORCH_STATE_DIR", None)
        else:
            os.environ["ORCH_STATE_DIR"] = old


class Tix:
    """A real TIX server on localhost, its owner (BrowserSim) and the relay tool every desk device runs."""

    def __init__(self, tix_dir: Path, work: Path):
        self.dir, self.work = tix_dir, work
        self.port = free_port()
        self.url = f"http://localhost:{self.port}"
        self.data = work / "tix-data"
        self.data.mkdir(parents=True)
        self.log = work / "tix.log"
        self.env = clean_env(FS_DATA_DIR=str(self.data), FS_PUBLIC_URL=self.url, FS_COOKIE_SECURE="0",
                             FS_SKILL_DIR=str(tix_dir / "skill" / "sharing"))
        self.proc = None
        self.sim = None
        self.tool = work / "bin" / "sharing"

    def start(self) -> "Tix":
        with open(self.log, "wb") as log:
            self.proc = _Popen([sys.executable, "-m", "uvicorn", "--factory", "fileshare.app:create_app",
                                "--host", "127.0.0.1", "--port", str(self.port), "--workers", "1"],
                               env=self.env, cwd=self.dir, stdout=log, stderr=subprocess.STDOUT)

        def healthy():
            if self.proc.poll() is not None:
                raise RuntimeError("TIX exited early:\n" + self.log.read_text())
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=1) as r:
                return r.status == 200
        until(healthy, 30, what="the TIX server")
        sys.path.insert(0, str(self.dir))
        from tests.helpers.browser_sim import BrowserSim  # tix's own scripted owner (same crypto as the browser)
        code = subprocess.run([sys.executable, "-m", "fileshare.admin", "setup-code"], env=self.env, cwd=self.dir,
                              capture_output=True, text=True, check=True).stdout.split()[-1]
        self.sim = BrowserSim(self.url)
        self.sim.setup(code)
        self.tool.parent.mkdir(parents=True)
        self.tool.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.dir}/skill/sharing/sharing.py" "$@"\n')
        self.tool.chmod(0o755)
        return self

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def presence(self) -> dict:
        """space id -> the presence row, as the owner's browser sees it."""
        r = self.sim.http.get("/api/presence")
        r.raise_for_status()
        return {s["id"]: s for s in r.json()["spaces"]}

    def tool_run(self, cwd: Path, *args: str, stdin: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.tool), *args], cwd=cwd, input=stdin, capture_output=True, text=True,
                              timeout=timeout, env=clean_env())

    def onboard(self, root: Path, name: str, project: str) -> None:
        """A desk device in `root`: the real handshake, approved by the owner, then a space for the workspace."""
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        code, token_id = self.sim.onboarding_code()
        with ThreadPoolExecutor(1) as ex:
            fut = ex.submit(self.sim.approve_when_pending, token_id)
            r = self.tool_run(root, "_handshake", "--code", "-", "--server", self.url, "--repo", str(root),
                              "--skill-dir", str(root / ".claude" / "skills" / "sharing"), "--device", name,
                              "--project", project, "--hostname", "host-" + name, stdin=code)
            fut.result(60)
        assert r.returncode == 0, r.stderr
        r = self.tool_run(root, "space", "create", "--label", name.title(), "--json")
        assert r.returncode == 0, r.stderr


class Dash:
    """The owner's own browser on the computer: the dashboard at its loopback address, token cookie held."""

    def __init__(self, url: str):
        self.url = url
        self.http = httpx.Client(base_url=url, headers={"Origin": url}, timeout=30, follow_redirects=False)

    def sign_in(self, token: str) -> None:
        r = self.http.get("/", params={"token": token})
        assert r.status_code == 303, r.text

    def get(self, path: str, **kw) -> httpx.Response:
        return self.http.get(path, **kw)

    def post(self, path: str, data: dict | None = None) -> httpx.Response:
        return self.http.post(path, data=data or {})

    # -- the Remote tab ----------------------------------------------------------------------------------------
    def offer(self, scope: str = "operate") -> str:
        """Press 'new pairing link': the link the owner opens on the other device."""
        r = self.post("/workspace/remote/offer", {"scope": scope})
        assert r.status_code == 303, r.text
        page = self.get(r.headers["location"]).text
        m = re.search(r"https?://[^\s\"'<>]+/remote/pair#v1\.[A-Za-z0-9._-]+", unescape(page))
        assert m, "no pairing link on the Remote tab"
        return m.group(0)

    def tab(self) -> str:
        return unescape(self.get("/workspace?tab=remote").text)

    def pending(self) -> list[tuple[str, str]]:
        """[(device id, fingerprint)] waiting for approval."""
        found = re.findall(r"Fingerprint: <code>([^<]+)</code>.*?pending/([0-9a-f]+)/reject", self.tab(), re.S)
        return [(did, fp) for fp, did in found]

    def approve(self, did: str, fingerprint: str, scope: str | None = None) -> httpx.Response:
        data = {"last_group": fingerprint.rsplit("-", 1)[-1]}
        if scope:
            data["scope"] = scope
            if scope == "type":
                data["allow_type"] = "1"
        return self.post(f"/workspace/remote/pending/{did}/approve", data)

    def devices(self) -> list[str]:
        return re.findall(r"/workspace/remote/devices/([0-9a-f]+)/revoke", self.tab())

    def set_scope(self, did: str, scope: str) -> httpx.Response:
        return self.post(f"/workspace/remote/devices/{did}/scope",
                         {"scope": scope, **({"allow_type": "1"} if scope == "type" else {})})

    def revoke(self, did: str) -> httpx.Response:
        return self.post(f"/workspace/remote/devices/{did}/revoke")


class Host:
    """One workspace: its folder, its dashboard process (the dashboard command with the remote flag) and its log."""

    def __init__(self, tix: Tix, work: Path, name: str, state_dir: Path, tickets=(), tmux_socket: str | None = None):
        self.tix, self.name, self.state_dir = tix, name, state_dir
        self.root = work / name
        self.log = work / f"{name}.log"
        self.tickets = tickets
        self.tmux_socket = tmux_socket
        self.proc = None
        self.dash: Dash | None = None
        self.port: int | None = None
        self.space = ""

    def prepare(self) -> "Host":
        from orch.addons import userfiles
        from orch.addons.discovery import custom_addons_dir
        from orch.addons.manifest import load_manifest
        from orch.testing import fake_workspace
        self.tix.onboard(self.root, self.name, "e2e")
        self.space = self.tix.tool_run(self.root, "space", "show", "--json").stdout
        import json
        self.space = json.loads(self.space)["space_id"]
        fake_workspace(self.root, customer=self.name.title(), prefix=self.name[:4].upper(), tickets=list(self.tickets))
        src = self.tix.dir / "addons" / "orch-tix"
        with state_env(self.state_dir):
            dest = custom_addons_dir() / "orch-tix"
            if not dest.exists():
                shutil.copytree(src, dest, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"))
                m = load_manifest(dest)
                userfiles.record_install("orch-tix", source={"type": "path", "path": str(src)}, version=m.version,
                                         requires_api=m.requires_api, folder=dest)
                userfiles.record_trust("orch-tix", dest, m)
            m = load_manifest(dest)
            userfiles.set_enabled(self.root, "orch-tix", True)
            userfiles.save_addon_config(self.root, "orch-tix", {"sharing_path": str(self.tix.tool)})
            if self.tmux_socket:
                userfiles.set_enabled(self.root, "terminals", True)
        return self

    def start(self, take_over: bool = False, timeout: float = 90) -> "Host":
        self.port = free_port()
        args = ["--remote", "--no-open", "--no-update", "--port", str(self.port)] + (["--take-over"] if take_over else [])
        env = clean_env(ORCH_STATE_DIR=str(self.state_dir), XDG_CONFIG_HOME=str(self.state_dir / "xdg"),
                        **({"E2E_TMUX_SOCKET": self.tmux_socket} if self.tmux_socket else {}))
        self.log.write_text("")
        with open(self.log, "ab") as log:
            self.proc = _Popen([sys.executable, str(LAUNCHER), *args], cwd=self.root, env=env, stdout=log,
                               stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

        def ready():
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name}: the dashboard exited ({self.proc.returncode}):\n{self.text()}")
            m = re.search(r"orch dashboard: (http://[^\s?]+)/\?token=(\S+)", self.text())
            return m and "reachable from your paired devices" in self.text() and m
        m = until(ready, timeout, what=f"{self.name}'s dashboard")
        self.dash = Dash(m.group(1))
        self.dash.sign_in(m.group(2))
        return self

    def text(self) -> str:
        return self.log.read_text(errors="replace") if self.log.exists() else ""

    def stop(self, sig=signal.SIGINT, wait: float = 60) -> int | None:
        if self.proc is None or self.proc.poll() is not None:
            return None if self.proc is None else self.proc.returncode
        self.proc.send_signal(sig)
        try:
            return self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()

    def kill(self) -> None:
        """A computer that vanishes: no goodbye, no lease release."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


class Tmux:
    """A private tmux server (its own socket name), so the person's `-L orch` server is never touched."""

    def __init__(self):
        self.socket = f"orch-e2e-{os.getpid()}"

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", self.socket, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def session(self, name: str, cwd: Path, command: str = "cat") -> None:
        r = self.run("new-session", "-d", "-s", name, "-c", str(cwd), "-x", "120", "-y", "30", command)
        assert r.returncode == 0, r.stderr

    def screen(self, name: str) -> str:
        return self.run("capture-pane", "-p", "-t", name).stdout

    def close(self) -> None:
        self.run("kill-server")
