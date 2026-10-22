"""Tier A, phase 1: the per-user host service (H7, D-P2-2, §13 phase 1).

``orch host enable`` (and ``orch host register`` when no host answers) installs the release (D-P2-4), writes the unit
for the person's own service manager and starts it; the OS then keeps the host running. A CLI call never runs the host
itself: it only asks the service manager to.

* **macOS:** a LaunchAgent ``~/Library/LaunchAgents/io.orch.host.plist`` in the person's GUI domain
  (``launchctl bootstrap gui/<uid>``; ``kickstart`` when it is loaded already).
* **Linux:** a systemd user unit ``~/.config/systemd/user/orch-host.service`` (``daemon-reload``, ``enable``,
  ``start``). The user manager searches its own config path, which is ``~/.config`` unless the manager itself was
  started with another ``XDG_CONFIG_HOME``; the CLI's ``XDG_CONFIG_HOME`` is not used for the unit (a dev kit that sets
  one would otherwise write the unit where ``systemctl --user`` never looks).
* **Windows and anything else:** no service; the workspace stays in direct mode (owner re-plan, 12 Oct 2026).

The unit runs ``<release>/current/bin/orch-host run --state <state> --run <run>`` with ``python -I``. In tier A the run
directory is ``<state>/run``, the one the CLI, ``orch sign`` and the dashboard look in; the root-owned
``/Users/Shared/orch/run`` and ``/run/orch`` of tier B come with the setup helper (H3c, phase 3).

**One service per UID.** The label is fixed, so a unit that is installed for another state dir, run dir or interpreter
is never replaced silently (``enable`` refuses and names the fields; ``orch host enable --replace`` replaces it, and
``register`` never does). **No dev code as the service:** an interpreter or ``orch`` package inside a git work tree, a
``.worktrees`` checkout or a workspace is refused unless ``--dev``: the unit would keep running whatever that tree holds
next.

Every outside effect goes through :class:`Manager` (the commands through ``run``, the files under ``home``), so tests
use a fake runner and a temporary home. As a second fence, the default runner refuses to call ``launchctl`` or
``systemctl`` at all while ``PYTEST_CURRENT_TEST`` is set or ``ORCH_NO_SERVICE_MANAGER=1``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import units

__all__ = [
    "Manager",
    "Plan",
    "Unsupported",
    "dev_source",
    "differences",
    "plan",
    "run_dir_for",
    "unit_fields",
    "wait_for_control",
]

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]
#: seconds ``enable`` waits for the started host's control socket
START_WAIT = 20.0


class Unsupported(Exception):
    """No per-user service manager on this platform: direct mode."""


@dataclass(frozen=True)
class Plan:
    kind: str  # "launchd" | "systemd"
    unit_path: Path
    content: bytes
    state: Path
    run: Path
    release: Path
    uid: int
    #: the service's launchd label and systemd unit name (the host's by default; the runner's, H11a)
    launchd_label: str = units.LAUNCHD_LABEL
    systemd_name: str = units.SYSTEMD_NAME

    @property
    def label(self) -> str:
        return self.launchd_label if self.kind == "launchd" else self.systemd_name

    @property
    def program_name(self) -> str:
        """The name macOS shows for the item in Login Items & Extensions (the launcher's file name)."""
        return "orch-runner" if self.launchd_label.endswith(".runner") else "orch-host"


def run_dir_for(state: Path, given: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The run directory of a tier-A host: ``--run``, else ``ORCH_HOST_RUN_DIR`` (dev, tests), else ``<state>/run``;
    always absolute, so a unit file never carries a path relative to the directory ``enable`` ran in."""
    if given is not None:
        return Path(given).absolute()
    if env and env.get("ORCH_HOST_RUN_DIR"):
        return Path(env["ORCH_HOST_RUN_DIR"]).absolute()
    return (Path(state) / "run").absolute()


def plan(
    env: Mapping[str, str],
    state: Path,
    run: Path,
    release: Path,
    *,
    platform: str | None = None,
    python: Path | None = None,
    uid: int | None = None,
) -> Plan:
    """The unit for this platform, as files and bytes; nothing is written."""
    platform = platform or sys.platform
    python = python or Path(sys.executable)
    uid = os.getuid() if uid is None else uid
    home = env.get("HOME")
    if not home or not home.startswith("/"):
        raise Unsupported("HOME is not set to an absolute path: no per-user service")
    log_dir = Path(state) / "host"
    if platform == "darwin":
        path = Path(home) / "Library" / "LaunchAgents" / f"{units.LAUNCHD_LABEL}.plist"
        content = units.launchd_plist(python, release, Path(state), log_dir=log_dir, run_dir=Path(run))
        return Plan("launchd", path, content, Path(state), Path(run), release, uid)
    if platform.startswith("linux"):
        path = Path(home) / ".config" / "systemd" / "user" / units.SYSTEMD_NAME
        content = units.systemd_unit(python, release, Path(state), run_dir=Path(run)).encode()
        return Plan("systemd", path, content, Path(state), Path(run), release, uid)
    raise Unsupported(f"no per-user service on {platform}: workspaces stay in direct mode")


def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ORCH_NO_SERVICE_MANAGER") == "1":
        raise OSError(f"{argv[0]} refused: the service manager is off (a test, or ORCH_NO_SERVICE_MANAGER=1)")
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)


def _failed(argv: Sequence[str], res: subprocess.CompletedProcess[str]) -> str:
    return f"{' '.join(argv)} failed ({res.returncode}): {(res.stderr or res.stdout or '').strip()}"


def _arg_after(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args[:-1] else None


def _unquote_systemd(line: str) -> list[str]:
    import re

    words = re.findall(r'"((?:[^"\\]|\\.)*)"', line)
    return [re.sub(r"\\(.)", r"\1", w).replace("%%", "%").replace("$$", "$") for w in words]


def _launcher_interpreter(path: str) -> str:
    """The interpreter a launcher's shebang names (``#!<python> -I``); the launcher's own path when it cannot be read,
    so an unreadable launcher never looks like the expected interpreter."""
    try:
        first = Path(path).read_text().split("\n", 1)[0]
    except (OSError, UnicodeDecodeError):
        return path
    return first[2:].removesuffix(" -I") if first.startswith("#!") and first.endswith(" -I") else path


def unit_fields(kind: str, content: bytes, release: Path | None = None) -> dict[str, str | None] | None:
    """``{python, state, run}`` of a unit file's command, or ``None`` when it cannot be read as one of ours."""
    try:
        if kind == "launchd":
            import plistlib

            args = [str(a) for a in plistlib.loads(content).get("ProgramArguments") or []]
        else:
            line = next(x for x in content.decode().splitlines() if x.startswith("ExecStart="))
            args = _unquote_systemd(line[len("ExecStart=") :])
    except Exception:  # noqa: BLE001 - not a unit of ours: the caller treats it as different
        return None
    if not args:
        return None
    # a launchd unit that runs the release's launcher directly (macOS lists it by that name): its interpreter is the
    # launcher's shebang. Only the expected launcher counts as one (``release`` given); any other program is its own
    # "python", so it differs
    python = args[0]
    if (
        kind == "launchd"
        and release is not None
        and Path(args[0]) in (release / "bin" / "orch-host", release / "bin" / "orch-runner")
    ):
        python = _launcher_interpreter(args[0])
    return {"python": python, "state": _arg_after(args, "--state"), "run": _arg_after(args, "--run")}


def differences(unit: Plan) -> list[str] | None:
    """Which of ``python``, ``state``, ``run`` the installed unit has otherwise than ``unit`` would (``[]``: the same;
    ``None``: no unit installed). A unit that cannot be read counts as different in every field."""
    try:
        have = unit.unit_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return ["python", "state", "run"]
    old, new = unit_fields(unit.kind, have, unit.release), unit_fields(unit.kind, unit.content, unit.release)
    if old is None or new is None:
        return ["python", "state", "run"]
    return [k for k in ("python", "state", "run") if old[k] != new[k]]


_INSTALLED = frozenset({"site-packages", "dist-packages"})


def _in_checkout(path: Path, stop: frozenset[Path] = frozenset(), *, git_alone: bool = False) -> bool:
    """Inside a ``.worktrees`` checkout, or inside a git work tree with a Python project between it and ``path`` (a
    ``pyproject.toml`` or ``setup.py`` on the way up). A git work tree alone is not enough: Homebrew's prefix is one,
    and so is a home directory kept in git; an installed copy (under ``site-packages``) is never a checkout. The walk
    ends before ``stop`` (the home directory, for an interpreter's environment: pipx and uv tool environments live
    under it, and a home directory kept in git with a ``pyproject.toml`` must not make them checkouts). With
    ``git_alone`` (a virtual environment, judged as the interpreter) a git work tree is enough: a venv inside a
    checkout runs that checkout's changing dependencies even when orch itself was installed without ``-e``."""
    if _INSTALLED & set(path.parts):
        return False
    project = False
    for d in [path, *path.parents]:
        if d in stop:
            return False
        if d.name == ".worktrees":
            return True
        project = project or (d / "pyproject.toml").is_file() or (d / "setup.py").is_file()
        if (project or git_alone) and (d / ".git").exists():
            return True
    return False


def dev_source(python: Path, package: Path, roots: Sequence[str] = ()) -> str | None:
    """Why the interpreter or the ``orch`` package is development code that must not become the service (``None``:
    it is not): inside a git work tree or a ``.worktrees`` checkout (a ``.venv`` of a checkout included), or inside a
    registered workspace."""
    # The interpreter is judged by its environment (a venv's bin/python links to its base interpreter, so the link is
    # not followed): ``sys.prefix`` for the running one, else the directory above ``bin``.
    env = Path(sys.prefix) if python == Path(sys.executable) else Path(python).parent.parent
    try:  # the home directory as written and resolved (a symlinked home must still end the walk)
        home = frozenset({Path(os.path.abspath(Path.home())), Path.home().resolve()})
    except (OSError, RuntimeError):
        home = frozenset()
    for what, path, stop in (("the interpreter", env, home), ("the interpreter", Path(python), home),
                             ("the orch package", Path(package), frozenset())):  # fmt: skip
        try:
            if path is env or what == "the orch package":
                real = path.resolve()
            else:  # the interpreter file itself: resolve its directory, never its own link
                real = path.parent.resolve() / path.name
        except OSError:
            continue
        venv = what == "the interpreter" and (real / "pyvenv.cfg").is_file()
        if _in_checkout(real, stop, git_alone=venv):
            return f"{what} ({path}) is in a source checkout"
        for root in roots:
            try:
                if real == Path(root).resolve() or Path(root).resolve() in real.parents:
                    return f"{what} ({path}) is inside the workspace {root}"
            except OSError:
                continue
    return None


def label_disabled(text: str, label: str) -> bool:
    """Whether ``launchctl print-disabled`` output lists ``label`` as disabled (``"io.orch.host" => disabled``; older
    macOS prints ``=> true``). A label that is not listed, or is enabled, is not disabled."""
    for line in text.splitlines():
        m = re.fullmatch(r'\s*"([^"]+)"\s*=>\s*(\w+)\s*', line)
        if m and m.group(1) == label:
            return m.group(2) in ("disabled", "true")
    return False


def background_off_message(name: str) -> str:
    """What the person is told when macOS turned the item off (plain words, no ids)."""
    return (
        f"macOS has turned off orch in the background. Open System Settings → General → Login Items & Extensions "
        f"and turn on {name}."
    )


class Manager:
    """Install, start and look at the unit of a :class:`Plan` through the person's own service manager."""

    #: how long ``start`` waits for a booted-out service to finish unloading, and how often it looks
    UNLOAD_SECONDS = 15.0
    UNLOAD_POLL = 0.25

    def __init__(
        self,
        unit: Plan,
        run: Runner | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.plan = unit
        self.run = run or _default_run
        self._clock = clock
        self._sleep = sleep

    def _wait_unloaded(self) -> None:
        """``launchctl bootout`` returns before the old service (and its agents) finished stopping; a bootstrap in that
        window fails with 5 (EIO). Wait, bounded, until launchd no longer lists the label."""
        deadline = self._clock() + self.UNLOAD_SECONDS
        while self.loaded():
            if self._clock() >= deadline:
                what = "runner" if self.plan.program_name == "orch-runner" else "host"
                raise OSError(f"the old {what} service is still stopping; wait a moment and run the command again")
            self._sleep(self.UNLOAD_POLL)

    # -- commands
    def _domain(self) -> str:
        return f"gui/{self.plan.uid}"

    def _target(self) -> str:
        return f"{self._domain()}/{self.plan.launchd_label}"

    def loaded(self) -> bool:
        if self.plan.kind == "launchd":
            return self.run(["launchctl", "print", self._target()]).returncode == 0
        # systemd: enabled and running are independent (a disabled unit can be active, a unit file can be gone while
        # the unit still runs), so either one counts as "there is a service here"
        if self.run(["systemctl", "--user", "is-enabled", self.plan.systemd_name]).returncode == 0:
            return True
        return self.active() is True

    def disabled_in_background(self) -> bool | None:
        """macOS only: whether the person (or macOS) turned this item off in System Settings, General, Login Items &
        Extensions, "Allow in the Background". launchd records that as a disabled override, which
        ``launchctl print-disabled gui/<uid>`` lists (read-only). ``None``: not macOS, or it cannot tell."""
        if self.plan.kind != "launchd":
            return None
        try:
            res = self.run(["launchctl", "print-disabled", self._domain()])
        except OSError:
            return None
        if res.returncode != 0:
            return None
        return label_disabled(res.stdout or "", self.plan.launchd_label)

    def active(self) -> bool | None:
        """Whether the service manager runs the host now (``None``: it cannot tell)."""
        if self.plan.kind == "launchd":
            res = self.run(["launchctl", "print", self._target()])
            if res.returncode != 0:
                return False
            return "state = running" in (res.stdout or "")
        res = self.run(["systemctl", "--user", "is-active", self.plan.systemd_name])
        out = (res.stdout or "").strip()
        if out in ("active", "activating", "reloading", "refreshing", "deactivating"):
            return True
        return False if out else None

    def main_pid(self) -> int | None:
        """The PID of the process the service manager runs now (``None``: it cannot tell, or nothing runs)."""
        if self.plan.kind == "launchd":
            res = self.run(["launchctl", "print", self._target()])
            found = (
                re.search(r"^(?:\t| {4})pid = ([0-9]+)\s*$", res.stdout or "", re.M) if res.returncode == 0 else None
            )
        else:
            res = self.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", self.plan.systemd_name])
            found = re.fullmatch(r"\s*([1-9]\d*)\s*", res.stdout or "")
        return int(found.group(1)) if found else None

    def installed(self) -> bool:
        try:
            return self.plan.unit_path.read_bytes() == self.plan.content
        except OSError:
            return False

    def install(self) -> bool:
        """Write the unit (0644, atomically) when it differs; returns whether it changed."""
        if self.installed():
            return False
        path = self.plan.unit_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.plan.content)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return True

    BOOTSTRAP_TRIES = 3
    BOOTSTRAP_PAUSE = 5.0

    def _bootstrap(self, ran: list[list[str]]) -> None:
        """``launchctl bootstrap`` with a bounded retry for exit 5 only: the first bootstrap of a new or changed login
        item can fail transiently (macOS is adding it to Background Items) and works a few seconds later."""
        argv = ["launchctl", "bootstrap", self._domain(), str(self.plan.unit_path)]
        for attempt in range(self.BOOTSTRAP_TRIES):
            ran.append(argv)
            res = self.run(argv)
            if res.returncode == 0:
                return
            if self.disabled_in_background():
                raise OSError(background_off_message(self.plan.program_name))
            if res.returncode != 5:
                break
            if attempt + 1 < self.BOOTSTRAP_TRIES:
                self._sleep(self.BOOTSTRAP_PAUSE)
                if self.loaded():  # the old service is still unloading: wait for it too
                    self._wait_unloaded()
        what = "runner" if self.plan.program_name == "orch-runner" else "host"
        msg = f"{' '.join(argv)} failed ({res.returncode}): {(res.stderr or res.stdout or '').strip()}"
        if res.returncode == 5:
            msg += (
                f". macOS may still be adding orch-{what} as a background item: check System Settings > General > "
                f"Login Items, then run orch {what} enable again"
            )
        raise OSError(msg)

    def start(self, *, changed: bool) -> list[list[str]]:
        """Load and start the unit (a changed unit is reloaded); returns the commands that ran."""
        ran: list[list[str]] = []

        def do(argv: list[str], *, check: bool = True) -> None:
            ran.append(argv)
            res = self.run(argv)
            if check and res.returncode != 0:
                if self.plan.kind == "launchd" and argv[1:2] == ["bootstrap"] and self.disabled_in_background():
                    raise OSError(background_off_message(self.plan.program_name))
                raise OSError(f"{' '.join(argv)} failed ({res.returncode}): {(res.stderr or res.stdout or '').strip()}")

        if self.plan.kind == "launchd":
            if self.loaded():
                if changed:  # launchd reads the plist at bootstrap only
                    do(["launchctl", "bootout", self._target()], check=False)
                    self._wait_unloaded()
                    self._bootstrap(ran)
                else:
                    do(["launchctl", "kickstart", self._target()])
            else:
                self._bootstrap(ran)
            return ran
        do(["systemctl", "--user", "daemon-reload"])
        do(["systemctl", "--user", "enable", self.plan.systemd_name])
        do(["systemctl", "--user", "restart" if changed else "start", self.plan.systemd_name])
        return ran

    def restart_command(self) -> list[str]:
        """The command that restarts the loaded service at once, for this platform only."""
        if self.plan.kind == "launchd":
            return ["launchctl", "kickstart", "-k", self._target()]
        return ["systemctl", "--user", "restart", self.plan.systemd_name]

    def restart_now(self) -> list[list[str]]:
        """Restart the loaded service without waiting for the host (``launchctl kickstart -k``, ``systemctl --user
        restart``): the fallback when a graceful drain never finished. The host's state is crash-safe (the service
        manager restarts a crashed host the same way); the caller says so to the person."""
        argv = self.restart_command()
        res = self.run(argv)
        if res.returncode != 0:
            raise OSError(_failed(argv, res))
        return [argv]

    def stop(self) -> list[list[str]]:
        """Stop and unload the service, then remove the unit file (``orch host disable``). A loaded service that
        will not stop leaves the unit in place and raises. Returns the commands that ran. Never touches the state dir
        or the releases."""
        ran: list[list[str]] = []
        was_loaded = self.loaded()
        if self.plan.kind == "launchd":
            if was_loaded:
                argv = ["launchctl", "bootout", self._target()]
                ran.append(argv)
                res = self.run(argv)
                if res.returncode != 0 and self.loaded():
                    raise OSError(_failed(argv, res))
        elif was_loaded:  # enabled or running (or both): stop it whatever the unit file says
            argv = ["systemctl", "--user", "disable", "--now", self.plan.systemd_name]
            ran.append(argv)
            res = self.run(argv)
            if res.returncode != 0:  # e.g. the unit file is gone but the unit still runs: stop it by name
                ran.append(["systemctl", "--user", "stop", self.plan.systemd_name])
                self.run(ran[-1])
                if self.loaded():
                    raise OSError(_failed(argv, res))
        self.plan.unit_path.unlink(missing_ok=True)
        if self.plan.kind == "systemd":
            ran.append(["systemctl", "--user", "daemon-reload"])
            self.run(ran[-1])  # best effort: the unit file is gone either way
        return ran

    def status(self) -> dict[str, Any]:
        try:
            present = self.plan.unit_path.exists()
        except OSError:
            present = False
        diff = differences(self.plan)
        fields = None
        if present:
            try:
                fields = unit_fields(self.plan.kind, self.plan.unit_path.read_bytes(), self.plan.release)
            except OSError:
                fields = None
        return {
            "kind": self.plan.kind,
            "unit": str(self.plan.unit_path),
            "unit_present": present,
            "unit_current": self.installed(),
            "differs": diff or [],
            "serves": fields,
            "background_name": self.plan.program_name,
            "disabled_in_background": self.disabled_in_background() if present else None,
            "loaded": self.loaded() if present else False,
            "running": self.active() if present else False,
        }


def wait_for_control(path: Path, timeout: float = START_WAIT, *, clock: Callable[[], float] = time.monotonic) -> bool:
    """Whether the control socket at ``path`` answers a handshake within ``timeout`` seconds."""
    from .control_client import ControlClient

    deadline = clock() + timeout
    while True:
        try:
            ControlClient(path, timeout=2).close()
            return True
        except Exception:  # noqa: BLE001 - not there yet, refused, wrong owner: keep waiting, then say no
            if clock() >= deadline:
                return False
            time.sleep(0.1)


#: how many START_WAITs a drain gets before the service is restarted at once
DRAIN_WAIT_FACTOR = 2


def wait_for_release(
    manager: Manager,
    running: Callable[[], str | None],
    mine: str,
    wait: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until ``running()`` (the release digest the service reports) is ``mine``; a service the manager does not
    run is started once on the way."""
    deadline = time.monotonic() + wait
    started = False
    while time.monotonic() < deadline:
        if running() == mine:
            return True
        if not started and manager.active() is not True:
            started = True
            try:
                manager.start(changed=False)
            except OSError:
                return False
        sleep(0.2)
    return False


def restart_to_release(
    manager: Manager,
    running: Callable[[], str | None],
    mine: str,
    *,
    drain: Callable[[], bool] | None = None,
) -> str | None:
    """The one rule for every service that runs orch code (host, agent runner): the release it LOADED (``running``)
    must be ``mine``. ``drain`` (optional) asks it to finish its work and exit, and returns whether that was accepted;
    the service manager brings it back. Returns ``"current"`` (nothing to do), ``"drained"``, ``"forced"`` (after
    ``DRAIN_WAIT_FACTOR`` x START_WAIT, or with no drain, the service is restarted at once: crash-equivalent, work in
    flight may be interrupted and is recovered at start) or ``None`` (it did not come back on ``mine``)."""
    if running() == mine:
        return "current"
    if drain is not None and drain() and wait_for_release(manager, running, mine, START_WAIT * DRAIN_WAIT_FACTOR):
        return "drained"
    # A slow-starting new service may be in startup recovery when kickstart -k hits it; recovery is idempotent.
    try:
        manager.restart_now()
    except OSError:
        return None
    return "forced" if wait_for_release(manager, running, mine, START_WAIT) else None
