"""Service-manager units for the host (D-P2-2): the OS starts the host, never a CLI call.

These functions only build the file contents; installing them (``launchctl bootstrap``, ``systemctl enable``) is
the setup helper's job (H3c) and is never done here.

* macOS: LaunchAgent ``io.orch.host`` with ``RunAtLoad`` and ``KeepAlive``.
* Linux: a systemd user or system unit with ``Restart=always``, plus the ``tmpfiles.d`` lines that provision
  ``/run/orch/<host uid>`` (a user unit's ``RuntimeDirectory`` would live under ``$XDG_RUNTIME_DIR``, which agents
  cannot traverse).

The program is the release's launcher (``<release>/bin/orch-host``) run by ``python -I``, so neither ``PYTHONPATH``
nor the user site can change what the service imports.
"""

from __future__ import annotations

import plistlib
import shlex
from pathlib import Path

__all__ = ["LAUNCHD_LABEL", "SYSTEMD_NAME", "launchd_plist", "program_arguments", "systemd_unit", "tmpfiles_conf"]

LAUNCHD_LABEL = "io.orch.host"
SYSTEMD_NAME = "orch-host.service"


def program_arguments(python: Path, release: Path, state_dir: Path, run_dir: Path | None = None) -> list[str]:
    args = [str(python), "-I", str(release / "bin" / "orch-host"), "run", "--state", str(state_dir)]
    if run_dir is not None:
        args += ["--run", str(run_dir)]
    return args


def launchd_plist(python: Path, release: Path, state_dir: Path, *, log_dir: Path | None = None) -> bytes:
    doc: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": program_arguments(python, release, state_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "Umask": 0o077,
    }
    if log_dir is not None:
        doc["StandardOutPath"] = str(log_dir / "host.log")
        doc["StandardErrorPath"] = str(log_dir / "host.log")
    return plistlib.dumps(doc, sort_keys=True)


def systemd_unit(
    python: Path,
    release: Path,
    state_dir: Path,
    *,
    system: bool = False,
    user: str | None = None,
    group: str | None = None,
    host_uid: int | None = None,
) -> str:
    """A user unit (``systemctl --user``), or a system unit that runs as ``user`` (required with ``system``)."""
    if system and (not user or host_uid is None):
        raise ValueError("a system unit needs the host user and UID")
    if user == "root" or host_uid == 0:
        raise ValueError("the host never runs as root")
    run_dir = Path("/run/orch") / str(host_uid) if host_uid is not None else None
    exec_start = " ".join(shlex.quote(a) for a in program_arguments(python, release, state_dir, run_dir))
    lines = [
        "[Unit]",
        "Description=orch host",
        "After=network.target" if system else "After=default.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        "Restart=always",
        "RestartSec=1",
        "UMask=0077",
        "NoNewPrivileges=yes",
    ]
    if system:
        lines += [f"User={user}"] + ([f"Group={group}"] if group else [])
    lines += ["", "[Install]", "WantedBy=multi-user.target" if system else "WantedBy=default.target", ""]
    return "\n".join(lines)


def tmpfiles_conf(host_user: str, host_group: str, host_uid: int) -> str:
    """``/etc/tmpfiles.d/orch.conf``: ``/run/orch`` root 0755 and ``/run/orch/<uid>`` host 0711 (D-P2-6)."""
    return f"d /run/orch 0755 root root -\nd /run/orch/{host_uid} 0711 {host_user} {host_group} -\n"
