"""Tier A, phase 1: the per-user host service (H7, D-P2-2, §13 phase 1).

``orch host enable`` (and ``orch host register`` when no host answers) installs the release (D-P2-4), writes the unit
for the person's own service manager and starts it; the OS then keeps the host running. A CLI call never runs the host
itself: it only asks the service manager to.

* **macOS:** a LaunchAgent ``~/Library/LaunchAgents/io.orch.host.plist`` in the person's GUI domain
  (``launchctl bootstrap gui/<uid>``; ``kickstart`` when it is loaded already).
* **Linux:** a systemd user unit ``$XDG_CONFIG_HOME/systemd/user/orch-host.service`` (``systemctl --user enable
  --now``).
* **Windows and anything else:** no service; the workspace stays in direct mode (owner re-plan, 12 Oct 2026).

The unit runs ``<release>/current/bin/orch-host run --state <state> --run <run>`` with ``python -I``. In tier A the run
directory is ``<state>/run``, the one the CLI, ``orch sign`` and the dashboard look in; the root-owned
``/Users/Shared/orch/run`` and ``/run/orch`` of tier B come with the setup helper (H3c, phase 3).

Every outside effect goes through :class:`Manager` (the commands through ``run``, the files under ``home``), so tests
use a fake runner and a temporary home: nothing here touches the real user's launchd or systemd in a test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import units

__all__ = ["Manager", "Plan", "Unsupported", "plan", "run_dir_for", "wait_for_control"]

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

    @property
    def label(self) -> str:
        return units.LAUNCHD_LABEL if self.kind == "launchd" else units.SYSTEMD_NAME


def run_dir_for(state: Path, given: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """The run directory of a tier-A host: ``--run``, else ``ORCH_HOST_RUN_DIR`` (dev, tests), else ``<state>/run``."""
    if given is not None:
        return Path(given)
    if env and env.get("ORCH_HOST_RUN_DIR"):
        return Path(env["ORCH_HOST_RUN_DIR"])
    return Path(state) / "run"


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
        xdg = env.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg.startswith("/") else Path(home) / ".config"
        path = base / "systemd" / "user" / units.SYSTEMD_NAME
        content = units.systemd_unit(python, release, Path(state), run_dir=Path(run)).encode()
        return Plan("systemd", path, content, Path(state), Path(run), release, uid)
    raise Unsupported(f"no per-user service on {platform}: workspaces stay in direct mode")


def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)


class Manager:
    """Install, start and look at the unit of a :class:`Plan` through the person's own service manager."""

    def __init__(self, unit: Plan, run: Runner | None = None) -> None:
        self.plan = unit
        self.run = run or _default_run

    # -- commands
    def _domain(self) -> str:
        return f"gui/{self.plan.uid}"

    def _target(self) -> str:
        return f"{self._domain()}/{units.LAUNCHD_LABEL}"

    def loaded(self) -> bool:
        if self.plan.kind == "launchd":
            return self.run(["launchctl", "print", self._target()]).returncode == 0
        return self.run(["systemctl", "--user", "is-enabled", units.SYSTEMD_NAME]).returncode == 0

    def active(self) -> bool | None:
        """Whether the service manager runs the host now (``None``: it cannot tell)."""
        if self.plan.kind == "launchd":
            res = self.run(["launchctl", "print", self._target()])
            if res.returncode != 0:
                return False
            return "state = running" in (res.stdout or "")
        res = self.run(["systemctl", "--user", "is-active", units.SYSTEMD_NAME])
        out = (res.stdout or "").strip()
        return True if out == "active" else False if out else None

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

    def start(self, *, changed: bool) -> list[list[str]]:
        """Load and start the unit (a changed unit is reloaded); returns the commands that ran."""
        ran: list[list[str]] = []

        def do(argv: list[str], *, check: bool = True) -> None:
            ran.append(argv)
            res = self.run(argv)
            if check and res.returncode != 0:
                raise OSError(f"{' '.join(argv)} failed ({res.returncode}): {(res.stderr or res.stdout or '').strip()}")

        if self.plan.kind == "launchd":
            if self.loaded():
                if changed:  # launchd reads the plist at bootstrap only
                    do(["launchctl", "bootout", self._target()], check=False)
                    do(["launchctl", "bootstrap", self._domain(), str(self.plan.unit_path)])
                else:
                    do(["launchctl", "kickstart", self._target()])
            else:
                do(["launchctl", "bootstrap", self._domain(), str(self.plan.unit_path)])
            return ran
        do(["systemctl", "--user", "daemon-reload"])
        do(["systemctl", "--user", "enable", units.SYSTEMD_NAME])
        do(["systemctl", "--user", "restart" if changed else "start", units.SYSTEMD_NAME])
        return ran

    def status(self) -> dict[str, Any]:
        try:
            present = self.plan.unit_path.exists()
        except OSError:
            present = False
        return {
            "kind": self.plan.kind,
            "unit": str(self.plan.unit_path),
            "unit_present": present,
            "unit_current": self.installed(),
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
