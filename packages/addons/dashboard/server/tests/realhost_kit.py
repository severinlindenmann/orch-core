"""Drive a REAL orch workspace and host (core H6b) for the dashboard's real-host tests and demos.

Everything runs against a disposable state: an explicit `ORCH_STATE_DIR`, `HOME` and `XDG_CONFIG_HOME` under `base`
(with `.orch-dev` markers), never `~/.config/orch`. The person's terminal is a pty: `Pty` answers the passphrase prompt,
the confirmations of `orch setup` and the one Enter after a signing review, exactly as a person at a keyboard would
(the passphrase is the one setup generated, kept in <base>/passphrase-<dir>; it protects only a disposable sandbox).
The dashboard signs by one click only: `Workspace.convenience_on`, `Workspace.click_start` and `click_sign` drive it.

`base` must be SHORT and outside any sticky directory such as /tmp: the host refuses ancestors it does not trust, and a unix
socket path (`<state>/run/<ws>/host.sock`, `<state>/dashboard/ctl.sock`) holds about 100 bytes. `short_run_root()` gives
`~/.rh` unless `XDG_RUNTIME_DIR` is a real 0700 directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

CORE = Path(__file__).resolve().parents[4] / "core"
ADDON = Path(__file__).resolve().parents[2]
SESSION = "s_01M4QWK11VJHV7YX18VRQSZ1CA"
CLICK_CHILD = Path(__file__).resolve().with_name("click_child.py")


def python() -> str:
    return sys.executable


def short_run_root() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_dir() and (Path(runtime).stat().st_mode & 0o777) == 0o700:
        root = Path(runtime) / "orch-rh"
    else:
        root = Path.home() / ".rh"  # short: a workspace socket is <state>/run/<workspace id>/host.sock, 100 bytes max
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


class Pty:
    """Run a command with a controlling terminal and answer its prompts."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path, rules: list[tuple[str, Callable]], timeout=90):
        self.argv, self.env, self.cwd, self.rules, self.timeout = argv, env, cwd, rules, timeout
        self.output = ""
        self.code: int | None = None

    def run(self) -> Pty:
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child
            os.chdir(self.cwd)
            os.execvpe(self.argv[0], self.argv, self.env)
        buf, fired, t0, status = "", 0, time.time(), None
        try:
            while time.time() - t0 < self.timeout:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        data = b""
                    if not data:
                        break
                    buf += data.decode("utf-8", "replace")
                    self.output = buf.replace("\r", "")  # live: a driver in another thread reads the link from it
                    for rx, answer in self.rules:
                        found = None
                        for found in re.finditer(rx, buf[fired:].replace("\r", "")):
                            pass
                        if found:
                            reply = answer(found, buf.replace("\r", ""))
                            fired = len(buf)
                            if reply is not None:
                                time.sleep(0.1)
                                os.write(fd, (reply + "\n").encode())
                            break
                done, state = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = state
                    with contextlib.suppress(OSError):
                        while select.select([fd], [], [], 0.2)[0]:
                            data = os.read(fd, 65536)
                            if not data:
                                break
                            buf += data.decode("utf-8", "replace")
                    break
            else:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                self.output = buf.replace("\r", "")
                raise TimeoutError(f"{self.argv[-3:]} timed out:\n{self.output[-1500:]}")
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        if status is None:
            _, status = os.waitpid(pid, 0)
        self.code = os.waitstatus_to_exitcode(status)
        self.output = buf.replace("\r", "")
        return self


def _enter_after_review(m, t) -> str:
    """Core's one signing prompt: input in the first second after the review is discarded, so a person reads first."""
    time.sleep(1.2)
    return ""  # Pty sends the newline: a bare Enter


def confirm_rules(passphrase: str | Callable[[], str]) -> list[tuple[str, Callable]]:
    """Answers for a person's terminal. ``passphrase`` is the text or, better, a callable read when the prompt appears
    (the rules are built before `orch setup` has generated the passphrase)."""
    get = passphrase if callable(passphrase) else (lambda: passphrase)
    return [
        (r"[Pp]assphrase: $", lambda m, t: get()),
        (r"Press Enter to sign, any other key cancels\.\n?$", _enter_after_review),  # core's review_prompt
    ]


class Workspace:
    def __init__(
        self,
        base: Path,
        prefix: str = "DEMO",
        dirname: str = "billing",
        owner: str = "Demo Owner",
        state_name: str = "s",
    ):
        self.base, self.prefix, self.dirname, self.owner = Path(base), prefix, dirname, owner
        self.state, self.home = (
            self.base / state_name,
            self.base / "home",
        )  # short names: host sockets are <run>/<workspace id>/host.sock, 100 bytes max
        self.run = self.state / "run"
        self.root = self.base / dirname
        self.grant_secret: str | None = None
        self.session = SESSION
        self.host: subprocess.Popen | None = None
        self.log: Path = self.base / "host.log"

    # -- the disposable environment ---------------------------------------------------------------------------------
    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "ORCH_STATE_DIR": str(self.state),
            "ORCH_DEV": "1",
            "ORCH_HOST_RUN_DIR": str(
                self.run
            ),  # H4b: a registered workspace's CLI talks to the host on this socket dir
            "TERM": "xterm",
            "PYTHONWARNINGS": "ignore",
        }
        env.update(extra)
        return env

    @property
    def passphrase_file(self) -> Path:
        return self.base / f"passphrase-{self.dirname}"

    @property
    def passphrase(self) -> str:
        """The passphrase `orch setup` generated, from ``<base>/passphrase-<dirname>`` (written by :func:`setup_driver`).
        There is no default: a workspace that was not made by setup has none, and asking for it fails loudly."""
        try:
            text = self.passphrase_file.read_text().strip()
        except OSError as exc:
            raise RuntimeError(
                f"no generated passphrase: {self.passphrase_file} is missing (the workspace was not made by `orch setup`)"
            ) from exc
        if not text:
            raise RuntimeError(f"no generated passphrase: {self.passphrase_file} is empty")
        return text

    @passphrase.setter
    def passphrase(self, value: str) -> None:
        self.passphrase_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.passphrase_file.unlink(missing_ok=True)
        fd = os.open(self.passphrase_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")

    def rules(self) -> list[tuple[str, Callable]]:
        return confirm_rules(lambda: self.passphrase)

    def make_dirs(self) -> None:
        for path in (self.base, self.state, self.home, self.home / ".config", self.root):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        for marker in (self.state / ".orch-dev", self.root / ".orch-dev"):
            marker.touch()

    # -- commands ---------------------------------------------------------------------------------------------------
    def orch(self, *args: str) -> list[str]:
        return [python(), "-W", "ignore", "-m", "orch", *args]

    def human(self, *args: str, timeout: int = 90, ok: bool = True) -> str:
        """A person's command at their own terminal: the passphrase and the Enter after the review are typed for them."""
        run = Pty(self.orch(*args), self.env(), self.root, self.rules(), timeout).run()
        if ok and run.code != 0:
            raise RuntimeError(f"orch {' '.join(args)} exited {run.code}:\n{run.output[-1500:]}")
        return run.output

    def agent(self, *args: str, ok: bool = True) -> str:
        env = self.env(ORCH_GRANT=self.grant_secret or "", ORCH_SESSION=self.session)
        done = subprocess.run(self.orch(*args), env=env, cwd=self.root, capture_output=True, text=True, timeout=90)
        if ok and done.returncode != 0:
            raise RuntimeError(f"orch {' '.join(args)} exited {done.returncode}:\n{done.stdout}{done.stderr}")
        return done.stdout + done.stderr

    def unattended(self, *args: str, ok: bool = True) -> str:
        done = subprocess.run(
            self.orch(*args),
            env=self.env(ORCH_SESSION=self.session),
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if ok and done.returncode != 0:
            raise RuntimeError(f"orch {' '.join(args)} exited {done.returncode}:\n{done.stdout}{done.stderr}")
        return done.stdout + done.stderr

    # -- one click: the dashboard's only way to sign ----------------------------------------------------------------
    def convenience_on(self, timeout: int = 90) -> None:
        """The real `orch host convenience on`, in a pty: it moves the device key into the One click key file (the
        passphrase is typed once on the terminal) and switches the flag on. Needs the host and the workspace."""
        run = Pty(
            self.orch("host", "convenience", "on", "--workspace", self.ws_id, "--state", str(self.state)),
            self.env(), self.root, self.rules(), timeout,
        ).run()  # fmt: skip
        if run.code != 0:
            raise RuntimeError(f"orch host convenience on exited {run.code}:\n{run.output[-1500:]}")

    def click_start(self, children: list | None = None) -> Callable:
        """The ``start`` seam of the dashboard's click `Signer`: core's real signer, run by `click_child.py` with the
        file custody store of this disposable state (never the keychain). Each child is appended to ``children``."""

        def start(env):
            from orch.click.client import ClickClient

            parent, child = socket.socketpair()
            n = child.fileno()
            own = {**env, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")}
            own["ORCH_HOST_RUN_DIR"] = str(self.run)
            proc = subprocess.Popen(
                [sys.executable, "-I", str(CLICK_CHILD), "signer", "--fd", str(n)],
                pass_fds=(n,), close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, env=own,
            )  # fmt: skip
            child.close()
            if children is not None:
                children.append(proc)
            return ClickClient(parent, proc)

        return start

    # -- bring-up ---------------------------------------------------------------------------------------------------
    def host_answers(self) -> bool:
        """Whether a host (this object's or another process's) answers on the control socket of this run directory."""
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(self.run / "control" / "host.sock"))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def init(self) -> None:
        """Create the workspace with the real `orch setup --no-browser` (V3-D38: the only way). The host must be running
        (:meth:`start_host`): setup creates the workspace through it and registers it. A person's browser is played by
        :func:`setup_driver` over the setup socket; the terminal answers are typed in the pty."""
        self.make_dirs()
        if not self.host_answers():
            raise RuntimeError("orch setup needs the host: call start_host() first")
        setup_driver(self)
        self.stop_dashboard()  # `orch setup` leaves the wizard's dashboard server running: no caller may inherit it

    @property
    def dashboard_ctl(self) -> Path:
        return self.state / "dashboard" / "ctl.sock"

    def stop_dashboard(self, wait: float = 15.0) -> None:
        """Stop the dashboard server `orch setup` started for its wizard, through the server's own `stop` command (its
        control socket under this workspace's state dir: no pkill, no PIDs guessed), and wait until the socket is gone.
        That server's signer uses the keychain store, not the kit's file store, so a later caller that signs through
        it gets KeyNotFound (a 409 "uncertain"). Fails loudly if it does not stop."""
        venv = ADDON / "server" / ".venv" / "bin" / "python"
        env = {**self.env(), "PYTHONPATH": str(ADDON / "server" / "src")}
        done = subprocess.run(
            [str(venv) if venv.exists() else sys.executable, "-m", "orch_dashboard", "stop"],
            env=env, cwd=self.root, capture_output=True, text=True, timeout=30, check=False,
        )  # fmt: skip
        deadline = time.monotonic() + wait
        while self.dashboard_ctl.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.dashboard_ctl.exists():
            raise RuntimeError(
                f"the setup dashboard did not stop (exit {done.returncode}): {(done.stdout + done.stderr)[-400:]}"
            )

    @property
    def config(self) -> dict:
        return json.loads((self.root / ".orch" / "config.json").read_text())

    @property
    def ws_id(self) -> str:
        return self.config["workspace"]["id"]

    @property
    def person(self) -> str:
        return self.config["members"][0]["person"]

    def grant(self, hours: int = 24) -> str:
        out = self.human("grant", "--hours", str(hours), "--label", "demo agent")
        self.grant_secret = re.search(r"ORCH_GRANT=(\S+)", out).group(1)
        return self.grant_secret

    def install_dashboard(self) -> None:
        """The addon package and its signed `serve_http` grant: the dashboard server refuses an ungranted package."""
        target = self.root / ".orch" / "addons" / "dashboard"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy(ADDON / "orch-addon.json", target / "orch-addon.json")
        self.human("addon", "grant", "dashboard")

    def start_host(self) -> int:
        log = open(self.log, "ab")
        self.host = subprocess.Popen(
            self.orch("host", "run", "--foreground", "--state", str(self.state), "--run", str(self.run)),
            env=self.env(),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        control = self.run / "control" / "host.sock"
        for _ in range(150):
            if self.host.poll() is not None:
                raise RuntimeError(f"the host exited {self.host.returncode}:\n{self.log.read_text()[-1500:]}")
            if control.exists():
                with contextlib.suppress(OSError):
                    probe = socket.socket(socket.AF_UNIX)
                    probe.connect(str(control))
                    probe.close()
                    return self.host.pid
            time.sleep(0.2)
        raise RuntimeError("the host did not open its control socket")

    def stop_host(self) -> None:
        """Stop only the host this object started, by its own PID."""
        if self.host is None:
            return
        if self.host.poll() is None:
            self.host.terminate()
            try:
                self.host.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.host.kill()
                self.host.wait(timeout=5)
        self.host = None

    def register(self) -> None:
        """Pin the workspace and this UID's person in the running host (it must be running: the host owns the registry)."""
        done = subprocess.run(
            self.orch(
                "host",
                "register",
                str(self.root),
                "--peer",
                f"{os.getuid()}=person:{self.person}",
                "--run",
                str(self.run),
                "--state",
                str(self.state),
            ),
            env=self.env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if done.returncode != 0 or '"ok": true' not in done.stdout:
            raise RuntimeError(f"host register failed: {done.stdout}{done.stderr}")

    def bring_up(self, seeding: Callable[[Workspace], object] | None = None) -> object:
        """start the host, `orch setup`, grant, dashboard addon, then `seeding` through the direct CLI, then the host serves it.

        Seeding runs before `register` on purpose: since core H4b (#409) the CLI of a registered workspace talks to the
        host, but `orch task done` still answers not_implemented there (H3b2, #412), and seeding done tasks needs it.
        After `register` the same CLI writes through the running host (used for the live-update checks).
        Leaves the host running.
        """
        self.make_dirs()
        self.start_host()
        self.init()  # `orch setup` registers the workspace with the running host
        self.grant()
        self.install_dashboard()
        return seeding(self) if seeding else None


# -- `orch setup` without a browser: the page played over the setup socket ----------------------------------------------
_LINK = re.compile(r"http://127\.0\.0\.1:(\d{1,5})/#launch=([A-Za-z0-9_-]{20,100})&to=")


def setup_driver(ws: Workspace, *, timeout: int = 180) -> None:
    """Run the real `orch setup --no-browser` in a pty and answer it like a person: read the single-use link from the
    terminal, claim it on the setup socket, then walk the same routes the page calls (profile, secrets, review, create,
    progress, finish). Test-only: the product has no scripted path. Sets ``ws.passphrase`` to the generated one."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("orch_setup_test_helpers", CORE / "tests" / "setup" / "helpers.py")
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)  # the unix-socket HTTP client of core's setup tests, loaded by path
    call = helpers.call
    fp: dict[str, list[str] | None] = {"words": None}  # the review's fingerprint, set after /review
    problems: list[str] = []

    def confirm_tty(m, text):
        """R3: the person compares the three words on the terminal with the page's. Answer only if they are the same."""
        words = fp["words"]
        shown = re.findall(r"should show these three words:\n\n {4}(.+)\n", text)
        if not words or not shown or shown[-1].split() != list(words):
            problems.append(f"the terminal shows {shown[-1:] or 'no words'}, the review has {words}")
            return "no"  # declines: nothing is created
        return m.group(1)

    rules: list[tuple[str, Callable]] = [
        (r"If they match, type (\S+): $", confirm_tty)
    ]  # the rest once the passphrase is known
    env = ws.env()
    if not (Path(sys.executable).parent / "orch-dashboard").is_file():
        # running outside the dashboard venv (the e2e kit uses the core venv): `orch setup` finds the dashboard on PATH
        venv_bin = ADDON / "server" / ".venv" / "bin"
        env["PATH"] = f"{venv_bin}{os.pathsep}{env['PATH']}"
    pty_run = Pty(ws.orch("setup", "--no-browser", "--role", "workspace"), env, ws.root, rules, timeout)
    box: dict[str, object] = {}

    def work() -> None:
        try:
            pty_run.run()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller below
            box["exc"] = exc

    thread = threading.Thread(target=work, name="setup-pty", daemon=True)
    thread.start()

    def fail(what: str) -> RuntimeError:
        extra = f" [{'; '.join(problems)}]" if problems else ""
        return RuntimeError(f"orch setup: {what}{extra}\n{pty_run.output[-1500:]}")

    deadline = time.time() + timeout
    link = None
    while link is None:
        if time.time() > deadline or not thread.is_alive():
            raise fail("no setup link on the terminal")
        link = _LINK.search(pty_run.output.replace("\n", ""))  # the tty wraps a long link
        time.sleep(0.2)
    port = int(link.group(1))  # the link's own code is the dashboard's; the setup server's is in its 0600 code file
    sock = str(ws.state / "setup" / "setup.sock")
    code = json.loads((ws.state / "setup" / "setup.code").read_text())["code"]
    host = f"127.0.0.1:{port}"

    def post(route: str, body: object | None = None, token: str | None = None) -> dict:
        status, out, _ = call(sock, "POST", route, body if body is not None else {}, token=token, host=host)
        if status != 200:
            raise fail(f"{route} -> {status} {out}")
        return out

    def get(route: str, token: str) -> dict:
        status, out, _ = call(sock, "GET", route, token=token, host=host)
        if status != 200:
            raise fail(f"{route} -> {status} {out}")
        return out

    def shutdown() -> None:
        with contextlib.suppress(Exception):  # best effort: do not leave the setup child waiting after a failure
            call(sock, "POST", "/shutdown", {}, host=host)

    try:
        _walk(ws, post, get, fail, thread, box, pty_run, rules, fp, problems, deadline, port, code)
    except BaseException:
        shutdown()
        raise


def _walk(ws, post, get, fail, thread, box, pty_run, rules, fp, problems, deadline, port, code) -> None:
    token = post("/claim", {"code": code, "port": port})["token"]
    api = "/api/setup/"
    profile = {
        "name": ws.owner, "workspace_name": ws.owner, "prefix": ws.prefix, "addons": [], "method": "terminal",
        "agent_hours": 8, "agent_grant": False, "relay": False,
    }  # fmt: skip
    post(api + "profile", profile, token)
    secrets = post(api + "secrets/reveal", None, token)
    words = {str(p): secrets["recovery_words"][p - 1] for p in secrets["check_positions"]}
    if not post(api + "secrets/confirm", {"words": words}, token).get("ok"):
        raise fail("the recovery words were refused")
    ws.passphrase = secrets["passphrase"]
    ws.recovery_code = " ".join(
        secrets["recovery_words"]
    )  # the 24 words: hub join/mode/leave ask for them (C9 owner proof)
    rules.extend(ws.rules())  # built after the passphrase is set (the same list object the Pty reads)
    review = post(api + "review", None, token)
    fp["words"] = review["fingerprint"]
    create = {"review_id": review["review_id"], "digest": review["digest"]}
    run_id = post(api + "create", create, token)["run_id"]
    while True:
        if time.time() > deadline or not thread.is_alive():
            raise fail("the run did not finish")
        progress = get(api + f"progress?run_id={run_id}", token)
        if progress["error"]:
            raise fail(f"the run failed: {progress['error']} {progress['reasons']}")
        if progress["done"]:
            break
        time.sleep(0.3)
    post(api + "finish", None, token)
    thread.join(30)
    if thread.is_alive():
        raise fail("orch setup did not exit")
    if problems:
        raise fail("; ".join(problems))
    if "exc" in box:
        raise fail(f"driver error {box['exc']!r}")
    if pty_run.code != 0:
        raise fail(f"exited {pty_run.code}")


def sandbox_bring_up(
    ws: Workspace,
    sandbox_id: str,
    *,
    relay_origin: str = "https://relay.dev.example",
    on_host: Callable[[Workspace], object] | None = None,
) -> None:
    """Make a dev-pair sandbox workspace (prefix ``SBX``) with `orch setup` (V3-D38), in the one place that knows the
    order: the sandbox files that make a host a dev host need ids that exist only after setup, so the first dev host is
    **pending** (the manifest has only ``id`` and ``root``; it admits the one fresh SBX genesis and has no dev powers),
    then: setup -> stop the host (own PID) -> bind person, workspace row and marker -> start the full dev host.

    ``ws`` must be ``Workspace(..., prefix="SBX", owner=devsandbox.owner_name(sandbox_id), state_name="state")`` and its
    ``orch`` must add ``--dev-sim-convenience`` to ``host run --foreground`` (as the hub scenarios do). The host started
    here keeps running; the caller grants, installs add-ons and stops it as before. ``on_host(ws)`` runs after each host
    start (the e2e kit records the host's pid there, so a run that dies half-way still stops it)."""
    from orch import devsandbox

    if ws.state != ws.base / "state":
        raise RuntimeError('a sandbox needs Workspace(..., state_name="state"): devsandbox keeps dev.json there')
    ws.make_dirs()
    devsandbox.write_manifest(ws.base, sandbox_id)
    (ws.state / "host").mkdir(mode=0o700, exist_ok=True)
    devsandbox.write_dev_json(ws.base, sandbox_id, relay_origin)
    ws.start_host()  # pending dev host
    if on_host:
        on_host(ws)
    ws.init()
    ws.stop_host()
    wid = ws.ws_id.removeprefix("w_")
    devsandbox.write_manifest(ws.base, sandbox_id, person_id=ws.person, workspaces=[(wid, ws.root)])
    devsandbox.write_marker(ws.root, sandbox_id, wid)
    ws.start_host()  # the full dev host proves S1 to S5 for the bound workspace
    if on_host:
        on_host(ws)


def click_sign(client, base: str, headers: dict, body: dict, *, dwell: float = 1.2) -> tuple[dict, dict]:
    """One click through the dashboard's routes under ``base`` (``/api/w/<ws>``): prepare, view, wait out the signer's
    1 s guard, sign. ``headers`` carry the session and the Origin. Returns (prepared, signed); fails on any refusal."""

    def post(step: str, payload: dict) -> dict:
        res = client.post(f"{base}/click/{step}", json=payload, headers=headers)
        if res.status_code != 200:
            raise AssertionError(f"click/{step} -> {res.status_code} {res.text}")
        return res.json()

    prepared = post("prepare", body)
    view = post("view", {"request_id": prepared["request_id"]})
    time.sleep(dwell)
    signed = post("sign", {"request_id": prepared["request_id"], "view_digest": view["view_digest"]})
    return prepared, signed


def new_session(ws: Workspace, n: int) -> None:
    """One agent session per ticket: a session holds one claim."""
    ws.session = f"s_01M4QWK11VJHV7YX18VRQSZ{n:03d}"[:29]


def key_of(output: str) -> str:
    return re.search(r"^ok (\S+-\d+) ", output, re.M).group(1)


def sections(ws: Workspace, key: str, **texts: str) -> None:
    for name, text in texts.items():
        ws.agent("section", "set", name, "--ref", key, "-m", text)


def ready_requirements(ws: Workspace, key: str, summary: str = "") -> None:
    sections(
        ws,
        key,
        context="Asked for by finance every month end.",
        out_of_scope="PDF export.",
        requirements=summary or "Do the thing described by the title.",
    )


def ready_plan(ws: Workspace, key: str) -> None:
    sections(ws, key, plan="One module, streamed output.", decisions="UTF-8, comma separated.")


def seed(ws: Workspace) -> dict[str, str]:
    """Tickets in the statuses the dashboard shows, with questions, a gate waiting, tasks and evidence.

    Returns the keys by role. `backlog` is not seeded here: `move` is a person operation the host serves only through
    `human.prepare` (the dashboard's hand-off), so a ticket is moved to backlog once the host runs.
    """
    keys: dict[str, str] = {}
    evidence = ws.base / "report.txt"
    evidence.write_text("invoices exported: 42\n")

    # testing (A): both gates approved, a task with its receipt and evidence, submitted. The verify gate needs a reviewer who
    # is not the worker, and this workspace has one person: the live phase gives it a policy (hand-off) and then a verdict.
    k = keys["verdict"] = key_of(
        ws.agent(
            "new",
            "Export billing CSV",
            "--type",
            "feature",
            "--priority",
            "high",
            "--label",
            "billing,export",
            "--size",
            "m",
        )
    )
    ws.agent("ac", "add", "CSV has one row per invoice", "--ref", k)
    ready_requirements(ws, k, "Finance can download the month's invoices as one CSV file.")
    ws.human("approve", k, "requirements")
    ready_plan(ws, k)
    ws.agent("task", "add", "Write the exporter", "--verify", "echo exported", "--proves", "AC1", "--ref", k)
    ws.human("approve", k, "plan")
    new_session(ws, 1)
    ws.agent("claim", k)
    ws.agent("task", "start", "T1")
    ws.agent("task", "done", "T1", "--run", "--artifact", str(evidence), "--ac", "AC1")
    sections(ws, k, verification="Ran the exporter on March data: 42 rows, one per invoice.")
    ws.agent("submit")

    # testing (B): submitted, waiting for the person's verdict
    k = keys["testing"] = key_of(
        ws.agent("new", "Retry failed webhooks", "--type", "bug", "--priority", "urgent", "--label", "webhooks")
    )
    ws.agent("ac", "add", "A failed delivery is retried three times", "--ref", k)
    ready_requirements(ws, k, "Webhook deliveries that fail are retried with backoff.")
    ws.human("approve", k, "requirements")
    ready_plan(ws, k)
    ws.agent("task", "add", "Add the retry loop", "--verify", "echo retried", "--proves", "AC1", "--ref", k)
    ws.human("approve", k, "plan")
    new_session(ws, 2)
    ws.agent("claim", k)
    ws.agent("task", "start", "T1")
    ws.agent("task", "done", "T1", "--run", "--artifact", str(evidence), "--ac", "AC1")
    sections(ws, k, verification="Killed the receiver twice; the sender retried and delivered.")
    ws.agent("submit")

    # in progress: claimed, one task done, one open, a blocking question to the person
    k = keys["in_progress"] = key_of(
        ws.agent("new", "Dark mode for the settings page", "--type", "feature", "--priority", "medium", "--label", "ui")
    )
    ws.agent("ac", "add", "The page follows the system theme", "--ref", k)
    ws.agent("ac", "add", "The choice is remembered", "--ref", k)
    ready_requirements(ws, k, "The settings page supports a dark theme.")
    ws.human("approve", k, "requirements")
    ready_plan(ws, k)
    ws.agent("task", "add", "Theme tokens", "--verify", "echo tokens", "--proves", "AC1", "--ref", k)
    ws.agent("task", "add", "Persist the choice", "--verify", "echo saved", "--proves", "AC2", "--ref", k)
    ws.human("approve", k, "plan")
    new_session(ws, 3)
    ws.agent("claim", k)
    ws.agent("task", "start", "T1")
    ws.agent("task", "done", "T1", "--run", "--artifact", str(evidence), "--ac", "AC1")
    ws.unattended(
        "ask",
        "Should the dark theme also apply to the printed invoice?",
        "--ref",
        k,
        "--options",
        "yes,no",
        "--rec",
        "no",
        "--why",
        "It changes the print stylesheet.",
    )
    keys["question"] = k

    # open, requirements complete: the requirements gate waits for the person (Today: needs you)
    k = keys["gate"] = key_of(
        ws.agent("new", "Audit log for member changes", "--type", "feature", "--priority", "medium")
    )
    ws.agent("ac", "add", "Every member change is listed with who and when", "--ref", k)
    ready_requirements(ws, k, "Owners can see who changed which member.")

    # open, requirements complete too: the person requests changes on this one
    k = keys["changes"] = key_of(ws.agent("new", "Bulk invoice import", "--type", "feature", "--priority", "low"))
    ws.agent("ac", "add", "Imports a CSV of 1000 invoices", "--ref", k)
    ready_requirements(ws, k, "Finance can import invoices in bulk.")

    # open, a question answered by the person
    k = keys["answered"] = key_of(ws.agent("new", "Cache the board query", "--type", "chore", "--priority", "low"))
    ws.unattended(
        "ask", "Which cache backend should the board use?", "--ref", k, "--options", "memory,redis", "--rec", "memory"
    )
    ws.human("answer", "Q1", "--option", "memory", "--ref", k)

    # open, plain (a ticket to move to backlog on the real host)
    keys["plain"] = key_of(ws.agent("new", "Rename the export button", "--type", "chore", "--priority", "low"))

    # closed
    k = keys["closed"] = key_of(ws.agent("new", "Support CSV with semicolons", "--type", "feature"))
    ws.human("close", k, "--resolution", "wont_do", "--message", "Finance uses the comma form.")
    return keys


# -- a demo for a person: `python tests/realhost_kit.py up|open|down [BASE]` --------------------------------------------


def _demo(argv: list[str]) -> int:
    command = argv[0] if argv else "up"
    base = Path(argv[1]) if len(argv) > 1 else Path.home() / "orch-rh-demo"
    ws = Workspace(base)
    pidfile = base / "host.pid"
    if command == "up":
        if base.exists():
            print(f"{base} exists: run `down` and remove it first", file=sys.stderr)
            return 1
        keys = ws.bring_up(seed)
        pidfile.write_text(str(ws.host.pid))
        (base / "keys.json").write_text(json.dumps(keys, indent=1))
        print(f"host pid {ws.host.pid}, workspace {ws.ws_id}, tickets {keys}")
        print(f"state {ws.state} (ORCH_STATE_DIR), passphrase '{ws.passphrase}'")
        print(f"next: {sys.executable} {__file__} open {base}")
        return 0
    if command == "open":
        env = ws.env()
        env["PYTHONPATH"] = str(ADDON / "server" / "src")
        run = Pty([sys.executable, "-m", "orch_dashboard", "open", "--print-url"], env, ws.root, [], 40).run()
        print(run.output)
        return run.code or 0
    if command == "down":
        if pidfile.exists():
            pid = int(pidfile.read_text())
            try:
                cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
                if "orch" in cmd and "host" in cmd:
                    os.kill(pid, signal.SIGTERM)
                    print(f"stopped host {pid}")
            except ProcessLookupError:
                pass
        subprocess.run(
            [sys.executable, "-m", "orch_dashboard", "stop"],
            env={**ws.env(), "PYTHONPATH": str(ADDON / "server" / "src")},
            capture_output=True,
        )
        return 0
    print("usage: realhost_kit.py up|open|down [BASE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_demo(sys.argv[1:]))
