"""The runner's per-UID service (D-P2-24), installed like the host's (H7, :mod:`orch.host.enable`).

* macOS: LaunchAgent ``io.orch.runner`` (``~/Library/LaunchAgents/io.orch.runner.plist``) in the person's GUI domain.
* Linux: systemd user unit ``orch-runner.service`` with ``Delegate=yes``, so the runner may create one child cgroup
  per run under its own (D-P2-29, :mod:`orch.runner.cgroup`).
* Anything else (Windows): no runner, no terminals, no start-agent (D-P2-38).

The unit runs ``<release>/current/bin/orch-runner run --state <state> --run <run>`` with ``python -I``. The units set
no umask: runs start with the person's umask, captured by ``orch runner enable`` (§12.5); the runner's own files are
written with explicit private modes. Installing goes
through :class:`orch.host.enable.Manager` and the same seam as the host (``orch.host.cli.SERVICE_MANAGER``): the
default command runner refuses ``launchctl``/``systemctl`` under pytest or ``ORCH_NO_SERVICE_MANAGER=1``, and the test
session's guard refuses unit files under the real home.
"""

from __future__ import annotations

import os
import plistlib
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch.host import enable as host_enable
from orch.host.units import systemd_quote

__all__ = [
    "LAUNCHD_LABEL",
    "SYSTEMD_NAME",
    "launchd_plist",
    "loaded_release",
    "plan",
    "program_arguments",
    "restart_if_stale",
    "systemd_unit",
    "write_loaded_release",
]

LAUNCHD_LABEL = "io.orch.runner"
SYSTEMD_NAME = "orch-runner.service"


def program_arguments(
    python: Path, release: Path, state_dir: Path, run_dir: Path, extra_args: tuple[str, ...] = ()
) -> list[str]:
    from orch.host.units import refuse_dev_flags

    args = [str(python), "-I", str(release / "bin" / "orch-runner"), "run", "--state", str(state_dir)]
    args += ["--run", str(run_dir), *extra_args]
    refuse_dev_flags(args)  # §11A.17.2: the runner unit never gets a dev flag either
    for arg in args:
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in arg):
            raise ValueError(f"a control character in a unit argument: {arg!r}")
    return args


def launchd_plist(
    python: Path, release: Path, state_dir: Path, run_dir: Path, extra_args: tuple[str, ...] = ()
) -> bytes:
    log = str(Path(state_dir) / "runner" / "runner.out")
    doc: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": program_arguments(python, release, state_dir, run_dir, extra_args),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",  # it parents the person's agents and shells: never throttled as background
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(doc, sort_keys=True)


def systemd_unit(python: Path, release: Path, state_dir: Path, run_dir: Path, extra_args: tuple[str, ...] = ()) -> str:
    exec_start = " ".join(systemd_quote(a) for a in program_arguments(python, release, state_dir, run_dir, extra_args))
    return "\n".join(
        [
            "[Unit]",
            "Description=orch runner",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={exec_start}",
            "Restart=always",
            "RestartSec=1",
            "Delegate=yes",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def plan(
    env: Mapping[str, str],
    state: Path,
    run: Path,
    release: Path,
    *,
    platform: str | None = None,
    python: Path | None = None,
    uid: int | None = None,
) -> host_enable.Plan:
    """The runner unit for this platform (nothing is written); :class:`orch.host.enable.Unsupported` elsewhere."""
    platform = platform or sys.platform
    python = python or Path(sys.executable)
    uid = os.getuid() if uid is None else uid
    home = env.get("HOME")
    if not home or not home.startswith("/"):
        raise host_enable.Unsupported("HOME is not set to an absolute path: no per-user service")
    labels = {"launchd_label": LAUNCHD_LABEL, "systemd_name": SYSTEMD_NAME}
    if platform == "darwin":
        path = Path(home) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        content = launchd_plist(python, release, Path(state), Path(run))
        return host_enable.Plan("launchd", path, content, Path(state), Path(run), release, uid, **labels)
    if platform.startswith("linux"):
        path = Path(home) / ".config" / "systemd" / "user" / SYSTEMD_NAME
        content = systemd_unit(python, release, Path(state), Path(run)).encode()
        return host_enable.Plan("systemd", path, content, Path(state), Path(run), release, uid, **labels)
    raise host_enable.Unsupported(f"no runner on {platform}: no terminals and no start-agent here (D-P2-38)")


def loaded_release_path(state: Path) -> Path:
    return Path(state) / "runner" / "release.json"


def current_release() -> str:
    """The digest of the code this process loaded (taken once: :func:`orch.host.release.code_digest` is cached)."""
    from orch.host.release import code_digest

    try:
        return code_digest()
    except OSError:
        return "unknown"


def write_loaded_release(state: Path, release: str, *, pid: int | None = None) -> None:
    """The running runner records the release it LOADED at start, where ``orch host enable`` / ``orch runner enable``
    read it (the host also gets it in ``runner.hello``). Best effort: a missing file reads as an older runner."""
    from orch.host.custody import write_private

    try:
        write_private(loaded_release_path(state), {"release": release, "pid": os.getpid() if pid is None else pid})
    except OSError:
        pass


def loaded_release(state: Path, *, pid: int | None = None) -> str | None:
    """The release the running runner recorded (``None``: no file, or an unreadable one: an older runner). With
    ``pid``, a file written by another process (a dev runner, or the process before a restart) reads as ``None``."""
    doc = _release_doc(state)
    if doc is None or (pid is not None and doc.get("pid") != pid):
        return None
    return doc["release"]


def _release_doc(state: Path) -> dict[str, Any] | None:
    from orch.host.custody import read_json
    from orch.host.errors import HostError

    try:
        doc = read_json(loaded_release_path(state), os.getuid())
    except (HostError, OSError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("release"), str) else None


def live_runs(state: Path) -> int | None:
    """How many runs (agents, terminals) the runner's persisted table holds as live (``None``: unreadable, which
    callers treat as "some")."""
    from orch.host.errors import HostError

    from .table import RunTable

    table = RunTable(Path(state) / "runner", os.getuid())
    try:
        table.load()
    except (HostError, OSError):
        return None
    return len(table.live())


def restart_if_stale(
    unit: Any, manager: Any, state: Path, mine: str, *, force: bool = False, wait: float = 3.0
) -> dict[str, Any] | None:
    """The runner service of ``unit``, when the service manager runs it with another release than ``mine``, is
    restarted by the same rule as the host (:func:`orch.host.enable.restart_to_release`), but only when it has no live
    runs (agents, terminals): a restart stops them all, so with live runs the person is told and chooses (``force``
    restarts anyway). A new start from a stale runner is refused by the host (``runner.stale``) meanwhile.
    ``None``: no runner runs here, or it is current (or cannot be told yet)."""
    import shlex
    import time

    if manager.active() is not True:
        return None
    pid = manager.main_pid()
    # The file counts only when the service's own process wrote it. Another pid (a dev runner, or the process before a
    # restart) is "not known yet": wait briefly for the fresh file, and never restart on that alone.
    deadline = time.monotonic() + wait
    while True:
        doc = _release_doc(state)
        if doc is None or pid is None or doc.get("pid") == pid:
            break
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)
    if doc is not None and doc["release"] == mine:
        return None
    command = shlex.join(manager.restart_command())
    live = live_runs(state)
    if live != 0 and not force:
        have = "running agents or terminals" if live is None else f"{live} running agents/terminals"
        return {
            "restarted": False,
            "restart_needed": True,
            "message": (
                f"the agent runner is from an older orch version and has {have}; it was not restarted. "
                f"Stop them, then run: {command} (or orch host enable --restart-runner to restart it now)"
            ),
        }
    how = host_enable.restart_to_release(manager, lambda: loaded_release(state, pid=None), mine)
    if how in ("drained", "forced"):
        return {
            "restarted": True,
            "message": (
                "restarted the agent runner on the new release; agents and terminals it had started were stopped"
            ),
        }
    return {
        "restarted": False,
        "restart_needed": True,
        "message": (
            "the agent runner still runs an older release and did not restart: to restart it now "
            f"(this stops its agents and terminals): {command}"
        ),
    }
