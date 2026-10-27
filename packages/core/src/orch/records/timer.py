"""The auto-mode timer (H11e part 2): ``python -I -m orch.records auto --state <state>`` every 5 minutes, started by
the person's own service manager, never by a CLI call or the host.

* **macOS:** LaunchAgent ``~/Library/LaunchAgents/io.orch.records.plist`` (``StartInterval`` 300, not at load) in the
  GUI domain (``launchctl bootstrap gui/<uid>``; ``bootout`` to remove).
* **Linux:** systemd user units ``~/.config/systemd/user/orch-records.service`` (``Type=oneshot``) and
  ``orch-records.timer`` (``OnBootSec=5min``, ``OnUnitActiveSec=5min``); ``enable --now`` / ``disable --now``.
* **Anything else:** no timer (``records.unsupported``).

The same fences as H7's host service: every outside effect goes through :class:`TimerManager` (the commands through
``run``, the files under ``home``), the default runner is :func:`orch.host.enable._default_run`, which refuses to call
``launchctl`` or ``systemctl`` while ``PYTEST_CURRENT_TEST`` is set or ``ORCH_NO_SERVICE_MANAGER=1``, and the test
suite's session guard refuses a unit under the real home. An interpreter or ``orch`` package in a source checkout is
refused as the timer's program (unless ``dev``): the timer would keep running whatever that tree holds next.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import RecordsError

__all__ = ["LAUNCHD_LABEL", "SYSTEMD_SERVICE", "SYSTEMD_TIMER", "TimerManager", "TimerPlan", "plan"]

LAUNCHD_LABEL = "io.orch.records"
SYSTEMD_SERVICE = "orch-records.service"
SYSTEMD_TIMER = "orch-records.timer"
INTERVAL_S = 300
Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def program_arguments(python: Path, state: Path) -> list[str]:
    args = [str(python), "-I", "-m", "orch.records", "auto", "--state", str(state), "--json"]
    for arg in args:
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in arg):
            raise RecordsError("records.invalid", "a control character in a timer argument")
    return args


@dataclass(frozen=True)
class TimerPlan:
    kind: str  # "launchd" | "systemd"
    files: tuple[tuple[Path, bytes], ...]
    uid: int
    state: Path | None = None


def unit_args(kind: str, content: bytes) -> list[str] | None:
    """The program arguments of an installed timer unit, or ``None`` when it is not one of ours."""
    try:
        if kind == "launchd":
            doc = plistlib.loads(content)
            args = doc.get("ProgramArguments") if isinstance(doc, dict) else None
            return [str(a) for a in args] if isinstance(args, list) else None
        from orch.host.enable import _unquote_systemd

        for line in content.decode(errors="replace").splitlines():
            if line.startswith("ExecStart="):
                return _unquote_systemd(line[len("ExecStart=") :])
    except Exception:  # noqa: BLE001 - an unreadable unit is "not ours"
        return None
    return None


def fields(args: list[str] | None) -> dict[str, str | None]:
    """``{state, interpreter, dev}`` of a timer's program arguments (H7's fields for the host unit)."""
    from orch.host.enable import dev_source

    if not args:
        return {"state": None, "interpreter": None, "dev": None}
    state = args[args.index("--state") + 1] if "--state" in args[:-1] else None
    interp = args[0]
    dev = "dev" if dev_source(Path(interp), Path(interp)) is not None else "release"
    return {"state": state, "interpreter": interp, "dev": dev}


def plan(
    env: Mapping[str, str],
    state: Path,
    *,
    platform: str | None = None,
    python: Path | None = None,
    uid: int | None = None,
    dev: bool = False,
) -> TimerPlan:
    """The timer's unit files for this platform; nothing is written."""
    from orch.host import units
    from orch.host.enable import dev_source

    platform = platform or sys.platform
    python = python or Path(sys.executable)
    uid = os.getuid() if uid is None else uid
    home = env.get("HOME")
    if not home or not home.startswith("/"):
        raise RecordsError("records.unsupported", "HOME is not an absolute path: no per-user timer")
    if not dev:
        import orch

        why = dev_source(python, Path(orch.__file__).parent)
        if why is not None:
            raise RecordsError("records.unsupported", f"{why}: it must not become the timer's program (or --dev)")
    args = program_arguments(python, Path(state))
    log = str(Path(state) / "person" / "records-timer.log")
    if platform == "darwin":
        doc = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": args,
            "StartInterval": INTERVAL_S,
            "RunAtLoad": False,
            "ProcessType": "Background",
            "Umask": 0o077,
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        path = Path(home) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        return TimerPlan("launchd", ((path, plistlib.dumps(doc, sort_keys=True)),), uid, Path(state))
    if platform.startswith("linux"):
        exec_start = " ".join(units.systemd_quote(a) for a in args)
        service = (
            "[Unit]\nDescription=orch records auto mode\n\n"
            f"[Service]\nType=oneshot\nExecStart={exec_start}\nUMask=0077\n"
        )
        timer = (
            "[Unit]\nDescription=orch records auto mode every 5 minutes\n\n"
            f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec=5min\nUnit={SYSTEMD_SERVICE}\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        d = Path(home) / ".config" / "systemd" / "user"
        files = ((d / SYSTEMD_SERVICE, service.encode()), (d / SYSTEMD_TIMER, timer.encode()))
        return TimerPlan("systemd", files, uid, Path(state))
    raise RecordsError("records.unsupported", f"no per-user timer on {platform}: run orch records commit by hand")


def _default_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    from orch.host.enable import _default_run as run

    return run(argv)


class TimerManager:
    """Install, start, stop and remove the timer through the person's own service manager."""

    def __init__(self, unit: TimerPlan, run: Runner | None = None) -> None:
        self.plan = unit
        self.run = run or _default_run

    def _target(self) -> str:
        return f"gui/{self.plan.uid}/{LAUNCHD_LABEL}"

    def _do(self, argv: list[str], *, check: bool = True) -> None:
        res = self.run(argv)
        if check and res.returncode != 0:
            raise RecordsError("records.timer_failed", f"{argv[0]} {argv[1]} failed ({res.returncode})")

    def _installed(self) -> tuple[Path, bytes] | None:
        """The installed program unit (the plist, or the ``.service``), or ``None``."""
        path = self.plan.files[0][0]
        try:
            return path, path.read_bytes()
        except OSError:
            return None

    def differences(self) -> list[str]:
        """The fields in which an installed timer differs from this plan (``state``, ``interpreter``, ``dev``);
        ``["unit"]`` for a unit that is not one of ours; ``[]`` when none is installed or it is the same."""
        have = self._installed()
        if have is None or have[1] == self.plan.files[0][1]:
            return []
        theirs = unit_args(self.plan.kind, have[1])
        if theirs is None:
            return ["unit"]
        mine = fields(unit_args(self.plan.kind, self.plan.files[0][1]))
        other = fields(theirs)
        diff = [k for k in ("state", "interpreter", "dev") if mine[k] != other[k]]
        return diff or ["arguments"]

    def install(self, *, replace: bool = False) -> bool:
        """Write the unit files (0644, atomically). An installed timer that differs (another state dir, interpreter,
        or dev versus release) is never replaced silently: ``records.timer_conflict`` names the fields, and
        ``replace`` (``auto on --replace``) overrides (H7 parity, Opus #433 finding 2). Returns whether one changed."""
        diff = self.differences()
        if diff and not replace:
            raise RecordsError(
                "records.timer_conflict",
                "a records timer is installed with another " + ", ".join(diff),
                hint="orch records auto on --replace installs this one instead (the other's workspaces stop)",
            )
        changed = False
        if self.plan.state is not None:
            # launchd opens the log inside <state>/person before the program runs and creates it if missing
            # loose (launchd: 0744), and custody then refuses the whole person directory
            from orch.privdir import make_private_dir

            make_private_dir(Path(self.plan.state) / "person")
        for path, content in self.plan.files:
            try:
                if path.read_bytes() == content:
                    continue
            except OSError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            try:
                os.fchmod(fd, 0o644)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(content)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            changed = True
        return changed

    def start(self, *, changed: bool) -> None:
        if self.plan.kind == "launchd":
            loaded = self.run(["launchctl", "print", self._target()]).returncode == 0
            if loaded and not changed:
                return
            if loaded:
                self._do(["launchctl", "bootout", self._target()], check=False)
            self._do(["launchctl", "bootstrap", f"gui/{self.plan.uid}", str(self.plan.files[0][0])])
            return
        self._do(["systemctl", "--user", "daemon-reload"])
        self._do(["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER])

    def remove(self) -> str:
        """Stop the timer and delete its files, **only** when the installed unit runs for this state dir (a missing
        unit is fine). A timer installed for another state dir is kept (``"kept"``): its workspaces keep running."""
        have = self._installed()
        if have is not None:
            theirs = fields(unit_args(self.plan.kind, have[1]))
            if theirs["state"] != str(self.plan.state):
                return "kept"
        if self.plan.kind == "launchd":
            self._do(["launchctl", "bootout", self._target()], check=False)
        else:
            self._do(["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER], check=False)
        for path, _content in self.plan.files:
            path.unlink(missing_ok=True)
        if self.plan.kind == "systemd":
            self._do(["systemctl", "--user", "daemon-reload"], check=False)
        return "removed"
