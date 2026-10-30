"""What orch runs in the background here, and how to stop and start each piece.

Person services (one set per person, shared by every workspace): the host, the agent runner, the records timer and the
dashboard. Server parts (only if installed here): hub, relay, publish. Everything touching the machine is a seam on
:class:`Env`, so the tests drive it with a fake command runner and a fake install runner."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from orch import childenv

__all__ = ["Env", "LifecycleError", "Svc", "clear_stopped", "discover", "start_svc", "stop_svc", "stopped_by_orch"]

PERSON = "person"
SERVER = "server"
ORDER_STOP = ("runner", "dashboard", "host", "records", "helper", "hub", "relay", "publish")
ORDER_START = ("publish", "relay", "hub", "helper", "host", "runner", "records", "dashboard")
PERSON_KEYS = ("host", "runner", "records", "dashboard")
#: how long a service gets to end its work (the runner stops every agent first) before it counts as still stopping
UNLOAD_WAIT = 45.0


class LifecycleError(Exception):
    """One step failed; the message is plain words."""


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=childenv.sanitize(os.environ))
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _answers(sock: Path) -> bool:
    """Whether something accepts connections on this unix socket (a host or dashboard started by hand)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(os.fspath(sock))
        return True
    except OSError:
        return False
    finally:
        s.close()


@dataclass
class Env:
    env: Mapping[str, str]
    out: TextIO
    err: TextIO
    platform: str = sys.platform
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else 1)
    run: Callable[[list[str]], tuple[int, str]] = _run
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    answers: Callable[[Path], bool] = _answers
    #: the install runner (launchd / systemd / pidfile) that started the parts; ``None``: picked from the record
    runner_for: Callable[[str | None], Any] | None = None
    dashboard_entry: Callable[[], list[str] | None] | None = None
    unload_wait: float = UNLOAD_WAIT
    #: process facts and verified signals (:mod:`orch.runner.procs`); the tests pass a fake
    procs: Any = None

    @property
    def home(self) -> Path:
        h = self.env.get("HOME") or self.env.get("USERPROFILE")
        return Path(h) if h else Path.home()

    @property
    def state(self) -> Path:
        from orch.ops.runtime import state_dir

        return state_dir(self.env)

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")
        self.out.flush()


@dataclass
class Svc:
    key: str  # host | runner | records | dashboard | hub | relay | publish
    label: str  # plain words
    group: str  # PERSON | SERVER
    kind: str  # launchd | systemd | part | hand | unsupported
    name: str = ""  # launchd label, systemd unit or install part
    path: Path | None = None  # the unit file
    installed: bool = True
    state: str = "stopped"  # running | stopped | unknown
    timer: bool = False  # runs on a schedule: "running" means loaded
    detail: str = ""
    runner_kind: str | None = None  # for parts: the runner that started it
    version: str = ""  # as `orch install status` shows it: the part's record, else the installed orch-core
    blocked: bool = False  # macOS turned it off in Login Items & Extensions (not `orch stop`)


# ------------------------------------------------------------------------------------------------------ discovery


def discover(e: Env) -> list[Svc]:
    """Every service orch has installed on this computer, with its state now."""
    from orch.host import units as host_units
    from orch.install.state import Layout, load
    from orch.records import timer as records_timer
    from orch.runner import service as runner_service

    out: list[Svc] = []
    home = e.home
    if e.platform == "darwin":
        agents = home / "Library" / "LaunchAgents"
        rows = [
            ("host", "the host service", host_units.LAUNCHD_LABEL, False),
            ("runner", "the agent runner", runner_service.LAUNCHD_LABEL, False),
            ("records", "the records timer", records_timer.LAUNCHD_LABEL, True),
        ]
        for key, label, name, timer in rows:
            path = agents / f"{name}.plist"
            out.append(_unit(e, "launchd", key, label, name, path, timer))
    elif e.platform.startswith("linux"):
        d = home / ".config" / "systemd" / "user"
        rows = [
            ("host", "the host service", host_units.SYSTEMD_NAME, False),
            ("runner", "the agent runner", runner_service.SYSTEMD_NAME, False),
            ("records", "the records timer", records_timer.SYSTEMD_TIMER, True),
        ]
        for key, label, name, timer in rows:
            out.append(_unit(e, "systemd", key, label, name, d / name, timer))
    else:  # Windows and anything else: no per-user host, runner or timer (direct mode)
        for key, label in (
            ("host", "the host service"),
            ("runner", "the agent runner"),
            ("records", "the records timer"),
        ):
            out.append(Svc(key, label, PERSON, "unsupported", installed=False))

    host = next((s for s in out if s.key == "host"), None)
    if host is not None and not host.installed and e.answers(e.state / "run" / "control" / "host.sock"):
        host.installed, host.kind, host.state = True, "hand", "running"
        host.detail = "started by hand, not a service"

    layout = Layout(e.env)
    st = load(layout) if layout.file.exists() else {"parts": {}}
    parts = st.get("parts") or {}
    dash = st.get("dashboard")
    labels = {"hub": "the hub", "relay": "the relay", "publish": "publish"}
    for key in ("hub", "relay", "publish"):
        rec = parts.get(key)
        if not isinstance(rec, dict):
            out.append(Svc(key, labels[key], SERVER, "part", name=key, installed=False))
            continue
        out.append(_part(e, key, labels[key], SERVER, rec))
    helper = st.get("helper")
    if isinstance(helper, dict):  # the hub's service helper, when the installer made one (orch.install.helper)
        out.append(_part(e, "helper", "the service helper", SERVER, helper, name="hub-helper"))
    if isinstance(dash, dict):
        out.append(_part(e, "dashboard", "the dashboard", PERSON, dash))
    elif os.path.lexists(e.state / "dashboard" / "ctl.sock") and e.answers(e.state / "dashboard" / "ctl.sock"):
        out.append(Svc("dashboard", "the dashboard", PERSON, "hand", state="running", detail="started by hand"))
    else:
        out.append(Svc("dashboard", "the dashboard", PERSON, "part", name="dashboard", installed=False))
    return out


def _dist_version(dist: str) -> str:
    from importlib import metadata

    for name in (dist, "orch-core"):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return ""


BLOCKED_HELP = "Allow orch in System Settings > General > Login Items & Extensions, then run orch start again."


def _unit(e: Env, kind: str, key: str, label: str, name: str, path: Path, timer: bool) -> Svc:
    s = Svc(key, label, PERSON, kind, name=name, path=path, timer=timer, version=_dist_version("orch-core"))
    if not os.path.lexists(path):
        s.installed = False
        return s
    s.state = _unit_state(e, s)
    s.blocked = s.state != "running" and blocked_by_macos(e, s.name)
    return s


def blocked_by_macos(e: Env, label: str) -> bool:
    """macOS only: ``label`` is listed as disabled by ``launchctl print-disabled`` and not because ``orch stop`` did it
    (System Settings, General, Login Items & Extensions, "Allow in the Background" is off)."""
    if e.platform != "darwin" or label in stopped_by_orch(e.state):
        return False
    from orch.host.enable import label_disabled

    rc, out = e.run(["launchctl", "print-disabled", f"gui/{e.uid}"])
    return rc == 0 and label_disabled(out, label)


def blocked_orch_labels(e: Env) -> list[str]:
    """Every ``io.orch*`` label macOS has turned off (not by ``orch stop``): what setup says is blocked."""
    if e.platform != "darwin":
        return []
    rc, out = e.run(["launchctl", "print-disabled", f"gui/{e.uid}"])
    if rc != 0:
        return []
    from orch.host.enable import label_disabled

    names = re.findall(r'^\s*"(io\.orch[^"]*)"\s*=>', out, re.M)
    mine = stopped_by_orch(e.state)
    return [n for n in dict.fromkeys(names) if n not in mine and label_disabled(out, n)]


def _unit_state(e: Env, s: Svc) -> str:
    if s.kind == "launchd":
        rc, out = e.run(["launchctl", "print", f"gui/{e.uid}/{s.name}"])
        if rc != 0:
            return "stopped"
        return "running" if s.timer or "state = running" in out else "stopped"
    rc, out = e.run(["systemctl", "--user", "is-active", s.name])
    word = out.strip().splitlines()[0] if out.strip() else ""
    return "running" if word in ("active", "activating", "reloading") else "stopped"


def _part(e: Env, key: str, label: str, group: str, rec: dict[str, Any], name: str | None = None) -> Svc:
    name = name or key
    kind = rec.get("runner") if isinstance(rec.get("runner"), str) else None
    ver = rec.get("version") if isinstance(rec.get("version"), str) else ""
    if not ver and key == "dashboard":
        ver = _dist_version("orch-dashboard")
    s = Svc(key, label, group, "part", name=name, detail=str(rec.get("url") or ""), runner_kind=kind, version=ver)
    try:
        s.state = "running" if _runner(e, kind).info(name).get("running") else "stopped"
        label_of = getattr(_runner(e, kind), "label", None)
        if s.state != "running" and kind == "launchd" and label_of is not None:
            # `orch stop` records a part by its name, not its launchd label: a part it stopped is stopped, not blocked
            s.blocked = name not in stopped_by_orch(e.state) and blocked_by_macos(e, label_of(name))
    except Exception:  # noqa: BLE001 - a broken service manager must not hide the other rows
        s.state = "unknown"
    return s


def _runner(e: Env, kind: str | None) -> Any:
    if e.runner_for is not None:
        return e.runner_for(kind)
    from orch.install import service
    from orch.install.state import Layout

    layout = Layout(e.env)
    cls = {"launchd": service.LaunchdRunner, "systemd": service.SystemdRunner, "pidfile": service.PidfileRunner}.get(
        kind or ""
    )
    return cls(layout) if cls is not None else service.pick_runner(layout, e.env)


# ----------------------------------------------------------------------------------------------- stopped marker


def stopped_by_orch(state: Path) -> set[str]:
    """The names (launchd labels, systemd units, part names) ``orch stop`` has stopped and not yet started."""
    try:
        doc = json.loads((Path(state) / "lifecycle" / "stopped.json").read_text())
        return {n for n in doc.get("stopped", []) if isinstance(n, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def clear_stopped(state: Path, name: str) -> None:
    """Forget that ``orch stop`` stopped ``name`` (something started it again)."""
    _write_marker(Path(state), stopped_by_orch(state) - {name})


def _mark(e: Env, name: str, stopped: bool) -> None:
    names = stopped_by_orch(e.state)
    _write_marker(e.state, names | {name} if stopped else names - {name})


def _write_marker(state: Path, names: set[str]) -> None:
    p = Path(state) / "lifecycle" / "stopped.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_text(json.dumps({"v": 1, "stopped": sorted(names)}) + "\n")
        p.chmod(0o600)
    except OSError:
        pass  # the marker only improves the wording of the table; the unit state is the truth


# ----------------------------------------------------------------------------------------------- stop and start


def stop_svc(e: Env, s: Svc) -> str:
    """Stop ``s`` and keep it stopped across a reboot. Returns a short word for the report."""
    if s.kind in ("unsupported",) or not s.installed:
        return "not installed"
    if s.kind == "hand":
        if s.key == "dashboard":
            entry = e.dashboard_entry() if e.dashboard_entry else None
            if entry:
                e.run([*entry, "stop"])
                return "stopped (it was started by hand: start it again with orch dashboard)"
        raise LifecycleError(f"{s.label} was started by hand, so orch cannot stop it; end that program yourself")
    was = s.state
    if s.kind == "part":
        r = _runner(e, s.runner_kind)
        r.pause(s.name)
        _wait(e, lambda: not r.info(s.name).get("running"), s)
        if s.key == "dashboard":  # a launcher started by hand holds the control socket as well
            entry = e.dashboard_entry() if e.dashboard_entry else None
            if entry and os.path.lexists(e.state / "dashboard" / "ctl.sock"):
                e.run([*entry, "stop"])
    elif s.kind == "launchd":
        target = f"gui/{e.uid}/{s.name}"
        e.run(["launchctl", "disable", target])  # survives a reboot; the plist stays
        e.run(["launchctl", "bootout", target])
        _wait(e, lambda: _unit_state(e, s) == "stopped", s)
    else:
        e.run(["systemctl", "--user", "disable", "--now", s.name])
        _wait(e, lambda: _unit_state(e, s) == "stopped", s)
    _mark(e, s.name, True)
    s.state = "stopped"
    return "stopped" if was == "running" else "kept stopped"


def start_svc(e: Env, s: Svc) -> str:
    if s.kind == "unsupported" or not s.installed:
        return "not installed"
    if s.kind == "hand":
        return "already running (started by hand)"
    if s.state == "running":
        _mark(e, s.name, False)
        return "already running"
    if s.kind == "part":
        try:
            _runner(e, s.runner_kind).resume(s.name)
        except RuntimeError as ex:
            raise LifecycleError(str(ex)) from None
    elif s.kind == "launchd":
        target = f"gui/{e.uid}/{s.name}"
        e.run(["launchctl", "enable", target])
        last = (0, "")
        for _ in range(5):  # bootout answers before the job is gone: a bootstrap right after it may fail with 5
            last = e.run(["launchctl", "bootstrap", f"gui/{e.uid}", str(s.path)])
            if last[0] == 0 or _unit_state(e, s) == "running":
                break
            e.sleep(1)
        else:
            if blocked_by_macos(e, s.name):
                raise LifecycleError(f"{s.label} is blocked by macOS. {BLOCKED_HELP}")
            raise LifecycleError(f"{s.label} did not start: {last[1].strip()[-200:]}")
    else:
        rc, out = e.run(["systemctl", "--user", "enable", "--now", s.name])
        if rc != 0:
            raise LifecycleError(f"{s.label} did not start: {out.strip()[-200:]}")
    if s.key == "host":
        sock = e.state / "run" / "control" / "host.sock"
        end = e.clock() + 20
        while not e.answers(sock) and e.clock() < end:
            e.sleep(0.3)
        if not e.answers(sock):
            raise LifecycleError("the host service started but does not answer yet; try orch services in a moment")
    _mark(e, s.name, False)
    s.state = "running"
    return "started"


def _wait(e: Env, done: Callable[[], bool], s: Svc) -> None:
    end = e.clock() + e.unload_wait
    while not done():
        if e.clock() >= end:
            raise LifecycleError(
                f"{s.label} is still stopping after {int(e.unload_wait)} seconds (it ends running "
                "agents first); run orch services to see, then orch stop again"
            )
        e.sleep(0.25)
