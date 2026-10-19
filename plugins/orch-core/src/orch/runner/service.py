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


def write_loaded_release(state: Path, release: str) -> None:
    """The running runner records the release it LOADED at start, where ``orch host enable`` / ``orch runner enable``
    read it (the host also gets it in ``runner.hello``). Best effort: a missing file reads as an older runner."""
    from orch.host.custody import write_private

    try:
        write_private(loaded_release_path(state), {"release": release, "pid": os.getpid()})
    except OSError:
        pass


def loaded_release(state: Path) -> str | None:
    """The release the running runner recorded (``None``: no file, or an unreadable one: an older runner)."""
    from orch.host.custody import read_json
    from orch.host.errors import HostError

    try:
        doc = read_json(loaded_release_path(state), os.getuid())
    except (HostError, OSError):
        return None
    release = doc.get("release") if isinstance(doc, dict) else None
    return release if isinstance(release, str) else None


def restart_if_stale(unit: Any, manager: Any, state: Path, mine: str) -> dict[str, Any] | None:
    """The runner service of ``unit``, when the service manager runs it with another release than ``mine``, is
    restarted at once (the same rule as the host: :func:`orch.host.enable.restart_to_release`). Everything the runner
    started (agents, terminals) is stopped by that. ``None``: no runner is running here, or it is current."""
    if manager.active() is not True:
        return None
    if loaded_release(state) == mine:
        return None
    how = host_enable.restart_to_release(manager, lambda: loaded_release(state), mine)
    if how in ("drained", "forced"):
        return {
            "restarted": True,
            "message": (
                "restarted the agent runner on the new release; agents and terminals it had started were stopped"
            ),
        }
    return {
        "restarted": False,
        "message": (
            "the agent runner still runs an older release and did not restart: to restart it now "
            f"(this stops its agents and terminals): {' '.join(manager.restart_command())}"
        ),
    }
