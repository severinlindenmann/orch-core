"""The runner's per-UID service (D-P2-24), installed like the host's (H7, :mod:`orch.host.enable`).

* macOS: LaunchAgent ``io.orch.runner`` (``~/Library/LaunchAgents/io.orch.runner.plist``) in the person's GUI domain.
* Linux: systemd user unit ``orch-runner.service`` with ``Delegate=yes``, so the runner may create one child cgroup
  per run under its own (D-P2-29, :mod:`orch.runner.cgroup`).
* Linux tier B (agents run as their own OS user, spec 13): the SYSTEM unit template ``orch-runner@.service`` that root
  installed (``orch-os-setup``, :func:`orch.setup.osusers.unit_runner`), instance = the person. The person's CLI never
  writes, starts or stops it (no root): :func:`plan_tier_b` describes it for status and for the checks that compare it,
  and the :class:`orch.host.enable.Manager` of kind ``systemd-system`` only reads it. A new release reaches the runner
  through the host's ``runner.restart`` command, never through ``systemctl`` (:func:`restart_if_stale`).
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orch.host import enable as host_enable
from orch.host.units import launcher_arguments, systemd_quote

__all__ = [
    "LAUNCHD_LABEL",
    "SYSTEMD_NAME",
    "agent_tier",
    "launchd_plist",
    "loaded_release",
    "plan",
    "plan_tier_b",
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
        # the launcher is the program, so macOS lists the item as "orch-runner" (see units.launcher_arguments)
        "ProgramArguments": launcher_arguments(release, state_dir, run_dir, extra_args, name="orch-runner"),
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


@dataclass(frozen=True)
class Tier:
    """Where agents run: ``"A"`` (as the person), ``"B"`` (as ``agent_user``, an OS user of their own) or ``"broken"``
    (the agent user exists but a fact failed; ``why`` is the first plain reason, and nothing may fall back to A)."""

    kind: str
    agent_user: str | None = None
    person: str | None = None
    why: str = ""


def agent_tier(state: Path, uid: int | None = None, *, facts: Any = None) -> Tier:
    """Read the answer from the system, never from a flag (:func:`orch.host.isolation.detect`). A platform without the
    separation, or no agent user, is tier A. ``broken`` must stop the caller: silently starting a per-user runner while
    an agent user exists would run agents as the person behind the person's back."""
    from orch.host import isolation
    from orch.setup.osfacts import RealOsFacts
    from orch.setup.osusers import AGENT_PREFIX

    uid = os.getuid() if uid is None else uid
    facts = facts if facts is not None else RealOsFacts()
    if facts.platform != "linux":
        return Tier("A")
    user = facts.user_by_uid(uid)
    if user is None:
        return Tier("A")
    found = isolation.detect(facts, user.name, uid, str(state))
    if found.status == "same_user":
        return Tier("A")
    if found.isolated:
        return Tier("B", AGENT_PREFIX + user.name, user.name)
    return Tier("broken", AGENT_PREFIX + user.name, user.name, found.summary)


def plan_tier_b(
    person: str, releases: Path, *, uid: int | None = None, platform: str | None = None
) -> host_enable.Plan:
    """The runner's system unit for ``person`` (tier B; nothing is written, nothing here can write it). ``releases``
    is the directory that holds ``current`` (the unit runs ``<releases>/current/bin/orch-runner``). The plan's state
    and run directories are the AGENT's own (``/var/lib/orch/agents/orch-agent-<person>/state``, ``/run/orch/agents/
    orch-agent-<person>``), never the person's. Raises :class:`orch.host.enable.Unsupported` off Linux or for a name
    that cannot be an instance (the unit template puts the prefix in front, so no instance can name root)."""
    from orch.setup import osusers

    platform = platform or sys.platform
    if not platform.startswith("linux"):
        raise host_enable.Unsupported(f"agents run as their own user only on Linux, not on {platform}")
    if not osusers.valid_person(person):
        raise host_enable.Unsupported(f"'{person}' cannot name an agent user")
    user = osusers.AGENT_PREFIX + person
    unit = osusers.unit_runner(str(releases))
    return host_enable.Plan(
        "systemd-system",
        Path(unit.path),
        unit.content.encode(),
        Path(osusers.AGENT_HOME_ROOT) / user / "state",
        Path(osusers.AGENT_RUN_ROOT) / user,
        Path(releases) / "current",
        os.getuid() if uid is None else uid,
        launchd_label=LAUNCHD_LABEL,
        systemd_name=f"orch-runner@{person}.service",
    )


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
    callers treat as "some"; also a missing table: a runner that predates it may run agents it never recorded)."""
    from orch.host.errors import HostError

    from .table import RunTable

    table = RunTable(Path(state) / "runner", os.getuid())
    if not os.path.lexists(table.path):
        return None  # a runner from before the run table existed (or one that never wrote it): not "no live runs"
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
    ``None``: no runner runs here, or it is current (or cannot be told yet).

    Tier B (a system unit, ``unit.kind == "systemd-system"``): ``None`` always. The loaded-release file is in the
    agent's private state directory, which the person cannot read, and the person cannot restart a system unit. The
    host learns the runner's release from its ``runner.hello`` and asks it to restart when idle (``runner.restart``)."""
    import shlex
    import time

    if unit.kind == "systemd-system":
        return None
    if manager.active() is not True:
        return None
    pid = manager.main_pid()
    if pid is None:  # the service manager does not say which process runs: the release file proves nothing
        return {
            "restarted": False,
            "message": (
                "could not tell which process the agent runner service runs, so its release was not checked and it "
                f"was not restarted. To restart it now (this stops its agents and terminals): "
                f"{shlex.join(manager.restart_command())}"
            ),
        }
    # The file counts only when the service's own process wrote it. Another pid (a dev runner, or the process before a
    # restart) is "not known yet": wait briefly for the fresh file, and never restart on that alone.
    deadline = time.monotonic() + wait
    while True:
        doc = _release_doc(state)
        if doc is None or doc.get("pid") == pid:
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

    def running() -> str | None:  # the file counts only when written by the service's CURRENT process (the new one
        now = manager.main_pid()  # after a restart; a dev runner's file, or the old process's, is never evidence)
        return None if now is None else loaded_release(state, pid=now)

    how = host_enable.restart_to_release(manager, running, mine)
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
