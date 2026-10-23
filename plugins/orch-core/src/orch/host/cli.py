"""``orch host …``: run the host, install a release, print unit files, and the control-socket commands.

``orch host run --foreground`` is the dev and test entry (run directory ``<state>/run``); the service manager runs
``orch-host run`` from a release (run directory ``/Users/Shared/orch/run/<uid>`` or ``/run/orch/<uid>``). A CLI call
never starts a host as a side effect (D-P2-2). ``orch host run --foreground --dev-runner`` (dev and tests only) also
admits a runner of the same UID that ``orch runner run --foreground`` announced (:mod:`orch.runner.devmode`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from .errors import HostError

__all__ = ["default_peers", "disable", "enable", "ensure_service", "main"]


class _Exact(argparse.ArgumentParser):
    """No abbreviated long option (``--dev-sim`` never means ``--dev-sim-convenience``), in every subcommand."""

    def __init__(self, *args: Any, **kw: Any) -> None:
        kw["allow_abbrev"] = False
        super().__init__(*args, **kw)


def _parser() -> argparse.ArgumentParser:
    p = _Exact(prog="orch host", description="the orch host service")
    sub = p.add_subparsers(dest="cmd", required=True, parser_class=_Exact)
    run = sub.add_parser("run", help="serve every registered workspace (the service manager runs this)")
    run.add_argument("--foreground", action="store_true", help="dev mode: run directory <state>/run")
    run.add_argument(
        "--dev-runner",
        action="store_true",
        help="dev and tests only, with --foreground: also admit a runner of your UID started with orch runner run "
        "--foreground (no service manager needed); never with a service run directory",
    )
    run.add_argument(
        "--dev-sim-convenience",
        action="store_true",
        help="dev only, with --foreground, inside a dev-pair sandbox (orch-v2-host 11A.17): accept Simulator "
        "convenience answers on throwaway SBX sandbox workspaces; never in a service unit",
    )
    run.add_argument("--state", type=Path)
    run.add_argument("--run", type=Path)
    inst = sub.add_parser("install", help="install this code as an immutable release")
    inst.add_argument("--version")
    unit = sub.add_parser("unit", help="print a launchd or systemd unit (installing it is the setup helper's job)")
    unit.add_argument("kind", choices=["launchd", "systemd", "tmpfiles"])
    unit.add_argument("--release", type=Path, required=False)
    unit.add_argument("--state", type=Path)
    unit.add_argument("--system", action="store_true")
    unit.add_argument("--user")
    unit.add_argument("--group")
    for name in ("list", "restart"):
        c = sub.add_parser(name)
        c.add_argument("--state", type=Path)
        c.add_argument("--run", type=Path)
        if name == "restart":
            c.add_argument("--json", action="store_true", help="machine-readable output")
    en = sub.add_parser("enable", help="install the release and the per-user service unit, start the host (tier A)")
    en.add_argument("--state", type=Path)
    en.add_argument("--run", type=Path)
    en.add_argument(
        "--replace",
        action="store_true",
        help="replace a service installed for another state dir, run dir or interpreter (its host stops)",
    )
    en.add_argument("--dev", action="store_true", help="allow an interpreter or orch package from a source checkout")
    en.add_argument(
        "--restart-runner",
        action="store_true",
        help="also restart an agent runner of an older release now, even with running agents and terminals (they stop)",
    )
    en.add_argument("--json", action="store_true", help="machine-readable output")
    dis = sub.add_parser(
        "disable",
        help="stop and unload the per-user service and remove its unit (the reverse of enable); the state dir stays",
    )
    dis.add_argument("--state", type=Path)
    dis.add_argument("--run", type=Path)
    dis.add_argument(
        "--purge-releases",
        action="store_true",
        help="also delete the installed release copies (the runner service uses them too); orch host enable "
        "installs a new one",
    )
    dis.add_argument(
        "--force", action="store_true", help="also remove a service installed for another state dir, run dir or setup"
    )
    dis.add_argument("--json", action="store_true", help="machine-readable output")
    for name in ("status", "doctor"):
        st = sub.add_parser(name, help="the host, the service, the registered workspaces and this directory's mode")
        st.add_argument("--state", type=Path)
        st.add_argument("--run", type=Path)
        st.add_argument("--json", action="store_true")
    reg = sub.add_parser(
        "register", help="serve a workspace through the host (host mode); starts the service if needed"
    )
    reg.add_argument("root")
    reg.add_argument(
        "--peer",
        action="append",
        metavar="UID=person:p_…|agent|worker",
        help="default: your UID as the person whose device key for this workspace is on this machine",
    )
    reg.add_argument("--no-service", action="store_true", help="never install or start the service")
    reg.add_argument("--dev", action="store_true", help="as orch host enable --dev, when it starts the service")
    reg.add_argument("--state", type=Path)
    reg.add_argument("--run", type=Path)
    unreg = sub.add_parser("unregister")
    unreg.add_argument("workspace_id")
    unreg.add_argument("--state", type=Path)
    unreg.add_argument("--run", type=Path)
    conv = sub.add_parser("convenience", help="click-to-sign per workspace (orch-v2-host §0.8.6); off by default")
    conv.add_argument("switch", choices=("on", "off", "show"))
    conv.add_argument("--workspace", required=False, help="workspace id or prefix (on/off)")
    conv.add_argument("--state", type=Path)
    rdr = sub.add_parser(
        "readers",
        help="reader records of a workspace (orch-v2-host §0.8.6 item 9.10): reset drops them all (fail closed), the "
        "operator step after a known state restore",
    )
    rdr.add_argument("action", choices=("reset",))
    rdr.add_argument("--workspace", required=True, help="workspace id or prefix")
    rdr.add_argument("--state", type=Path)
    out = sub.add_parser("outcome")
    out.add_argument("idem")
    out.add_argument("--state", type=Path)
    out.add_argument("--run", type=Path)
    return p


def _state(given: Path | None, env: Mapping[str, str]) -> Path:
    from orch.ops.runtime import state_dir

    return given.resolve() if given is not None else state_dir(env)


def _peers(items: list[str]) -> dict[str, dict[str, str]]:
    peers: dict[str, dict[str, str]] = {}
    for item in items:
        uid, _, spec = item.partition("=")
        cls, _, person = spec.partition(":")
        peers[uid] = {"class": cls, "person": person} if cls == "person" else {"class": cls}
    return peers


def _control_path(given: Path | None, state: Path, env: Mapping[str, str] | None = None) -> Path:
    """The control socket in the tier-A run directory (:func:`orch.host.enable.run_dir_for`: ``--run``, else
    ``ORCH_HOST_RUN_DIR``, else ``<state>/run``). The tier-B directories (``/Users/Shared/orch/run/<uid>``) are never
    preferred because something exists there: any local account can create that path (H7 review)."""
    from .enable import run_dir_for

    return run_dir_for(state, given, env) / "control" / "host.sock"


def _check_dev_runner(foreground: bool, run_dir: Path, env: Mapping[str, str]) -> None:
    """``--dev-runner`` (orch.runner.devmode) only for a host a person started in the foreground on a dev run
    directory: never without ``--foreground`` (the units never pass it), never under the service manager's labels,
    never for a service run directory (``/Users/Shared/orch/run``, ``/run/orch``)."""
    from orch.runner import devmode
    from orch.runner.service import LAUNCHD_LABEL as RUNNER_LABEL

    from .units import LAUNCHD_LABEL

    if not foreground:
        raise HostError("invalid.input", "--dev-runner needs --foreground (dev and tests only)")
    if env.get("XPC_SERVICE_NAME") in (LAUNCHD_LABEL, RUNNER_LABEL):
        raise HostError("invalid.input", "--dev-runner is never for a service-managed host")
    if devmode.is_service_run_dir(run_dir):
        raise HostError("invalid.input", f"--dev-runner is for a dev run directory only, never {run_dir}")


def main(argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "run":
            from .enable import run_dir_for
            from .release import preload
            from .service import Host

            state = _state(args.state, env)
            run_dir = run_dir_for(state, args.run, env)  # tier A (phase 1); the units always pass --run
            if args.dev_runner:
                _check_dev_runner(args.foreground, run_dir, env)
            dev = None
            if args.dev_sim_convenience:  # §11A.17.2: the proof runs before anything binds; a failure never falls back
                from . import devmode

                devmode.check_launch(args.foreground, env, run_dir, state)
                dev = devmode.prove(state, env)
                from orch import devsandbox

                devsandbox.mark_dev_process()  # the click signer refuses in this process whatever its arguments
            preload()
            host = Host(state, run_dir, dev_runner=args.dev_runner, dev=dev)
            if dev is not None:
                from .devmode import status_line

                sys.stderr.write(f"orch host: {status_line(dev)}\n")
            if args.dev_runner:
                sys.stderr.write(
                    f"orch host: DEV RUNNER MODE: a foreground runner of UID {os.getuid()} announced in {run_dir} is "
                    "admitted without a service manager (dev and tests only)\n"
                )
            asyncio.run(host.run())
            return 0
        if args.cmd == "install":
            from .release import install_release

            out.write(f"{install_release(args.version)}\n")
            return 0
        if args.cmd == "unit":
            from . import units
            from .release import releases_dir

            release = args.release or releases_dir(dict(env)) / "current"
            state = _state(args.state, env)
            if args.kind == "launchd":
                out.write(units.launchd_plist(Path(sys.executable), release, state).decode())
            elif args.kind == "systemd":
                out.write(
                    units.systemd_unit(
                        Path(sys.executable),
                        release,
                        state,
                        system=args.system,
                        user=args.user,
                        group=args.group,
                        host_uid=os.getuid() if args.system else None,
                    )
                )
            else:
                out.write(units.tmpfiles_conf(args.user or str(os.getuid()), args.group or "root", os.getuid()))
            return 0
        if args.cmd == "convenience":
            return _convenience(args, env, out)
        if args.cmd == "readers":
            return _readers(args, env, out)
        if args.cmd == "enable":
            doc = enable(
                _state(args.state, env), env, run=args.run, replace=args.replace, dev=args.dev,
                runner="force" if args.restart_runner else "auto",
            )  # fmt: skip
            out.write(json.dumps(doc, sort_keys=True) + "\n" if args.json else human_text("enable", doc))
            return 0 if doc.get("ok") else int(doc.get("exit", 9))
        if args.cmd == "disable":
            doc = disable(_state(args.state, env), env, run=args.run, purge=args.purge_releases, force=args.force)
            out.write(json.dumps(doc, sort_keys=True) + "\n" if args.json else human_text("disable", doc))
            return 0 if doc.get("ok") else int(doc.get("exit", 9))
        if args.cmd in {"status", "doctor"}:
            from .status import render, report

            state = _state(args.state, env)
            rep = report(state, env, _control_path(args.run, state, env))
            out.write(json.dumps(rep, sort_keys=True) + "\n" if args.json else render(rep))
            return 0 if rep["ok"] else 1
        from .control_client import ControlClient, new_idem
        from .ownership import LOCK_WAIT_SECONDS

        state = _state(args.state, env)
        idem = new_idem() if args.cmd in {"register", "unregister"} else None
        service: dict[str, Any] | None = None
        peers: dict[str, dict[str, str]] = {}
        if args.cmd == "register":
            root = os.path.abspath(args.root)
            peers = _peers(args.peer) if args.peer else default_peers(state, Path(root), env)
            if not args.no_service:
                service = ensure_service(state, env, args.run, dev=args.dev)
                if not service.get("ok", True):
                    out.write(json.dumps({**service, "idem": idem}, sort_keys=True) + "\n")
                    return int(service.get("exit", 9))
        try:
            # A write may wait 60 s for the ownership lock: the client waits longer than the host does.
            with ControlClient(_control_path(args.run, state, env), timeout=LOCK_WAIT_SECONDS + 30) as client:
                if args.cmd == "list":
                    res = client.call("host.list")
                elif args.cmd == "restart":
                    res = client.call("host.restart")
                elif args.cmd == "register":
                    res = client.call("host.register", {"root": root, "peers": peers}, idem=idem)
                elif args.cmd == "unregister":
                    res = client.call("host.unregister", {"workspace_id": args.workspace_id}, idem=idem)
                else:
                    res = client.call("host.outcome", {"idem": args.idem})
        except (OSError, ConnectionError) as exc:
            error = {"code": "host.unavailable", "message": str(exc)[:400], "retryable": True}
            doc = {"ok": False, "error": error, **({"idem": idem} if idem else {})}
            out.write(json.dumps(doc, sort_keys=True) + "\n")  # with the idem: orch host outcome IDEM finds the result
            return 9
        if idem is not None:
            res = {**res, "idem": idem}
        if service is not None and service.get("started"):
            res = {**res, "service": service}
        if args.cmd == "restart" and not args.json:
            out.write(human_text("restart", res))
        else:
            out.write(json.dumps(res, sort_keys=True) + "\n")
        return 0 if res.get("ok") else int(res.get("exit", 1))
    except HostError as exc:
        out.write(json.dumps({"ok": False, "error": exc.error()}, sort_keys=True) + "\n")
        return exc.exit
    except (OSError, ConnectionError) as exc:
        error = {"code": "host.unavailable", "message": str(exc)[:400], "retryable": True}
        out.write(json.dumps({"ok": False, "error": error}, sort_keys=True) + "\n")
        return 9


def human_text(cmd: str, doc: Mapping[str, Any], *, subject: str = "orch host") -> str:
    """N18: ``doc`` (the result of ``enable``, ``disable`` or ``restart``, here or of ``orch runner``) as a few plain
    lines for a person; ``--json`` keeps the document for machines. Values come from orch's own result, but go through
    one-line cleaning anyway."""

    def one(v: Any) -> str:
        return " ".join(str(v).split())[:300]

    if not doc.get("ok"):
        err = doc.get("error") or {}
        return f"{subject} {cmd}: {one(err.get('message', 'failed'))} [{one(err.get('code', 'error'))}]\n"
    data = (doc.get("result") or {}).get("data") or {}
    lines: list[str] = []
    if cmd == "enable":
        if data.get("mode") == "direct":
            lines.append(
                f"No per-user service on this platform ({one(data.get('reason'))}); workspaces stay in direct mode."
            )
        else:
            lines.append(f"The orch host is running as a {one(data.get('service'))} service.")
            if data.get("changed"):
                lines.append("The service was installed or updated.")
            if data.get("message"):
                lines.append(one(data["message"]))
            if data.get("runner_message"):
                lines.append(one(data["runner_message"]))
    elif cmd == "disable":
        if data.get("service") is None:
            lines.append(f"No per-user service on this platform ({one(data.get('reason'))}); nothing to turn off.")
        else:
            lines.append(f"The orch host service ({one(data['service'])}) is off.")
            if data.get("purged_releases"):
                lines.append(f"Removed {len(data['purged_releases'])} installed release copies.")
            lines.append(f"Your workspaces and keys are kept ({one(data.get('state_kept'))}).")
            if data.get("note"):
                note = one(data["note"])
                lines.append(note[:1].upper() + note[1:] + ".")
    elif cmd == "restart":
        lines.append("The host is restarting; the service brings it back in a few seconds.")
    return "".join(line + "\n" for line in lines)


def _failure(code: str, message: str, exit_: int = 9, **extra: Any) -> dict[str, Any]:
    retryable = code == "host.unavailable"
    return {
        "ok": False,
        "exit": exit_,
        "error": {"code": code, "message": message[:400], "retryable": retryable},
        **extra,
    }


def enable(
    state: Path,
    env: Mapping[str, str],
    *,
    run: Path | None = None,
    replace: bool = False,
    dev: bool = False,
    runner: str = "auto",
) -> dict[str, Any]:
    """``orch host enable``: install the release (D-P2-4), write the per-user unit, start it and wait for the control
    socket (H7, tier A). Windows and other platforms without a per-user service answer ``mode: direct``. Refused before
    anything is written: a run dir whose socket paths would be too long, a control character in a unit value, dev code
    as the service (unless ``dev``), and a unit installed for another state dir, run dir or interpreter (unless
    ``replace``). ``runner``: ``auto`` restarts a stale agent runner only when it has no live runs, ``force`` always,
    ``skip`` never (the register path)."""
    from . import enable as svc
    from .custody import ensure_private_dir
    from .listener import SOCKET_PATH_LIMIT
    from .release import ReleaseError, _package_dir, install_release, releases_dir

    run_dir = svc.run_dir_for(state, run, env)
    try:
        unit = svc.plan(env, state, run_dir, releases_dir(dict(env)) / "current")
    except svc.Unsupported as exc:  # the platform check first: no release on a direct-mode platform
        return {"ok": True, "result": {"data": {"mode": "direct", "service": None, "reason": str(exc)}}}
    except ValueError as exc:  # a control character in a path (plistlib or the unit's own check)
        return _failure("invalid.input", f"the unit cannot hold this path: {exc}", 5)
    longest = run_dir / ("0" * 32) / "host.sock"
    if len(str(longest).encode()) > SOCKET_PATH_LIMIT:
        return _failure(
            "invalid.input",
            f"the run directory {run_dir} is too long for a socket path ({len(str(longest).encode())} > "
            f"{SOCKET_PATH_LIMIT} bytes); use a shorter ORCH_STATE_DIR or --run",
            5,
        )
    if not dev:
        from .custody import read_json
        from .registry import registry_path

        try:
            roots = [
                str(w.get("root")) for w in (read_json(registry_path(state), os.getuid()) or {}).get("workspaces") or []
            ]
        except HostError:
            roots = []
        why = svc.dev_source(Path(sys.executable), _package_dir(), roots)
        if why is not None:
            return _failure(
                "invalid.input",
                f"{why}: the service would keep running whatever that tree holds next. Run orch host enable from an "
                "installed orch, or pass --dev for a development host",
                5,
            )
    manager = SERVICE_MANAGER(unit)
    differs = svc.differences(unit)
    if not replace and (differs or (differs is None and _loaded(manager))):
        named = differs or ["unit"]  # no unit file here, but the label is loaded: from a unit file elsewhere
        what = ", ".join(named) + " differ" if differs else "unit differs: the label is loaded from another file"
        return _failure(
            "host.busy",
            f"a host service ({unit.label}) is already installed for another setup ({what}). Replacing it "
            "stops that host and the workspaces it serves; to do that on purpose: orch host enable --replace",
            4,
            differs=named,
        )
    try:
        install_release(target=releases_dir(dict(env)))
    except (ReleaseError, OSError) as exc:
        return _failure("internal", f"the release could not be installed: {exc}", 1)
    try:
        Path(state).mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_private_dir(Path(state) / "host", os.getuid())  # launchd opens host.log there before exec
        changed = manager.install()
        ran = manager.start(changed=changed)
    except (OSError, HostError) as exc:
        return _failure("host.unavailable", f"the {unit.kind} service did not start: {exc}")
    control = run_dir / "control" / "host.sock"
    if not svc.wait_for_control(control):
        where = (
            f"see {state / 'host' / 'host.log'}" if unit.kind == "launchd" else f"see journalctl --user -u {unit.label}"
        )
        return _failure(
            "host.unavailable",
            f"the {unit.kind} service started but no host answers on {control} after {int(svc.START_WAIT)} s; {where}",
        )
    data = {"mode": "host", "service": unit.kind, "unit": str(unit.unit_path), "changed": changed, "ran": ran,
            "control": str(control)}  # fmt: skip
    from .release import code_digest

    mine = code_digest()
    theirs = _host_release(control)
    if theirs != mine:  # a host that predates the welcome's release field is older by definition (``None``)
        if manager.active() is True:  # started by the service manager: restart it (never a process it did not start)
            how = _restart(manager, control, mine)
            if how in ("drained", "current"):
                data["restarted"] = mine
                data["message"] = f"restarted the host on the new release {mine}"
            elif how == "forced":
                data["restarted"] = mine
                data["forced"] = True
                data["message"] = (
                    "the host did not finish open work in time; it was restarted and a request in flight may have "
                    "been interrupted (its outcome is recovered at start)"
                )
            else:
                import shlex

                data["message"] = (
                    f"the host still runs the old release {theirs or 'unknown'} and did not restart. To restart it "
                    f"now (this interrupts requests in flight): {shlex.join(manager.restart_command())}"
                )
                data["restart_needed"] = True
        else:  # a foreground or dev host: not ours to kill
            data["restart_needed"] = True
            data["message"] = (
                f"the running host (release {theirs or 'unknown'}) is not under the service manager: "
                "stop it and start it again (or `orch host restart` when it is supervised) to run the new release"
            )
    if runner != "skip":
        _restart_stale_runner(state, env, run_dir, mine, data, force=runner == "force")
    return {"ok": True, "result": {"data": data}}


def _restart_stale_runner(
    state: Path, env: Mapping[str, str], run_dir: Path, mine: str, data: dict[str, Any], *, force: bool = False
) -> None:
    """The agent runner service, when one runs here with another release than this install, is restarted by the same
    rule as the host (its agents and terminals are stopped by that); the result says so."""
    from orch.runner import service as runner_service

    from . import enable as svc
    from .release import releases_dir

    try:
        unit = runner_service.plan(env, state, run_dir, releases_dir(dict(env)) / "current")
        stale = runner_service.restart_if_stale(unit, RUNNER_SERVICE_MANAGER(unit), state, mine, force=force)
    except (svc.Unsupported, ValueError, OSError):
        return
    if stale is not None:
        data["runner_restarted"] = stale["restarted"]
        data["runner_message"] = stale["message"]
        if stale.get("restart_needed"):
            data["restart_needed"] = True


def _host_release(control: Path) -> str | None:
    """The release digest the host on ``control`` reports in its welcome (``None``: none, or it does not say)."""
    from .control_client import ControlClient

    try:
        with ControlClient(control, timeout=2) as client:
            release = (client.welcome or {}).get("release")
    except Exception:  # noqa: BLE001 - nobody answers: nothing to compare
        return None
    return release if isinstance(release, str) else None


def _restart(manager: Any, control: Path, mine: str) -> str | None:
    """Make the host on ``control`` run release ``mine`` (:func:`orch.host.enable.restart_to_release`): drain it through
    its control socket (``orch host restart``) and let the service manager bring it back; an own or another's drain
    (``host.restarting``) is waited for, never cut short before the wait ends."""
    from .control_client import ControlClient
    from .enable import restart_to_release

    def drain() -> bool:
        try:
            with ControlClient(control, timeout=10) as client:
                frame = client.call("host.restart")
        except (OSError, ConnectionError):
            return False  # the drain did not start: the service is restarted at once
        return bool(frame.get("ok")) or (frame.get("error") or {}).get("code") == "host.restarting"

    return restart_to_release(manager, lambda: _host_release(control), mine, drain=drain)


def disable(
    state: Path, env: Mapping[str, str], *, run: Path | None = None, purge: bool = False, force: bool = False
) -> dict[str, Any]:
    """``orch host disable``: the reverse of :func:`enable` through the same unit plan and :class:`Manager`. Stops and
    unloads the service, removes the unit file; with ``purge`` also deletes the release copies. The state dir is kept.
    A unit installed for another state dir or run dir (or one that cannot be read, or a loaded label with no unit file
    here) is refused unless ``force``: the same "never silently" rule as ``enable``. Nothing to remove is a success."""
    from . import enable as svc
    from .release import ReleaseError, purge_releases, releases_dir

    run_dir = svc.run_dir_for(state, run, env)
    releases = releases_dir(dict(env))
    try:
        unit = svc.plan(env, state, run_dir, releases / "current")
    except svc.Unsupported as exc:
        return {"ok": True, "result": {"data": {"mode": "direct", "service": None, "reason": str(exc)}}}
    except ValueError as exc:
        return _failure("invalid.input", f"the unit cannot hold this path: {exc}", 5)
    if purge and not force:  # L2: the runner service runs from these release copies
        busy = _runner_service_busy(state, env, run_dir, releases)
        if busy:
            return _failure(
                "host.busy",
                f"the runner service ({busy}) runs from the release copies that --purge-releases deletes; "
                "run orch runner disable first, or pass --force",
                4,
            )
    manager = SERVICE_MANAGER(unit)
    differs = svc.differences(unit)
    other = [k for k in (differs or []) if k in ("state", "run")] if differs is not None else []
    foreign = differs is None and _loaded(manager)  # no unit file here, but the label is loaded from elsewhere
    if (other or foreign) and not force:
        what = ", ".join(other) + " differ" if other else "the label is loaded from another file"
        return _failure(
            "host.busy",
            f"the host service ({unit.label}) belongs to another setup ({what}); disabling it stops that host and "
            "the workspaces it serves. To do that on purpose: orch host disable --force",
            4,
            differs=other or ["unit"],
        )
    try:
        ran = manager.stop()
    except OSError as exc:
        return _failure("host.unavailable", f"the {unit.kind} service did not stop: {exc}")
    purged: list[str] = []
    if purge:
        try:
            purged = purge_releases(releases)
        except (ReleaseError, OSError) as exc:
            return _failure("internal", f"the service is disabled but the releases were not removed: {exc}", 1)
    data = {"service": unit.kind, "unit": str(unit.unit_path), "removed": differs is not None,
            "ran": ran, "state_kept": str(state), "purged_releases": purged if purge else None,
            "note": "registered workspaces answer host.unavailable until orch host enable"}  # fmt: skip
    return {"ok": True, "result": {"data": data}}


def _runner_service_busy(state: Path, env: Mapping[str, str], run_dir: Path, releases: Path) -> str | None:
    """The runner's service label when its unit is installed or loaded/active here (``None``: it is not)."""
    from orch.runner import service as runner_service

    from . import enable as svc

    try:
        unit = runner_service.plan(env, state, run_dir, releases / "current")
    except (svc.Unsupported, ValueError):
        return None
    try:
        if unit.unit_path.exists():
            return unit.label
    except OSError:
        return unit.label
    return unit.label if _loaded(RUNNER_SERVICE_MANAGER(unit)) else None


def _loaded(manager: Any) -> bool:
    try:
        return bool(manager.loaded())
    except OSError:
        return False


#: the service manager of a plan (tests replace it with a fake that never runs launchctl or systemctl)
def SERVICE_MANAGER(unit: Any) -> Any:  # noqa: N802 - a module-level seam, named like the class it builds
    from .enable import Manager

    return Manager(unit)


#: the runner's service manager (a seam like :data:`SERVICE_MANAGER`, so a fake host manager is never asked about it)
def RUNNER_SERVICE_MANAGER(unit: Any) -> Any:  # noqa: N802
    from .enable import Manager

    return Manager(unit)


def ensure_service(state: Path, env: Mapping[str, str], run: Path | None, *, dev: bool = False) -> dict[str, Any]:
    """Before ``register``: a host that answers is used as it is (a dev host, the running service); otherwise the
    per-user service is enabled (H7: registration turns host mode on). ``register`` never replaces a service installed
    for another setup (``enable`` refuses; the person decides with ``orch host enable --replace``).
    ``{"started": false}`` when nothing was done."""
    from .enable import wait_for_control

    if wait_for_control(_control_path(run, state, env), timeout=0):
        return {"started": False}
    doc = enable(state, env, run=run, dev=dev, runner="skip")  # a register never touches the runner
    if not doc.get("ok"):
        return doc
    data = doc["result"]["data"]
    if data.get("mode") == "direct":
        return _failure("host.unavailable", f"no host here and no service to start: {data['reason']}")
    return {"started": True, **data}


def default_peers(state: Path, root: Path, env: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """``--peer`` left out: this UID as the person whose device key for the workspace is on this machine (the one
    ``orch init`` made). The host checks the mapping again (a member with a valid ``decide`` device, F8)."""
    from orch.identity import device_ref
    from orch.ops import human
    from orch.ops.caps import Files
    from orch.ops.runtime import _config
    from orch.store import Store, StoreError
    from orch.store.pins import HostPins

    def refuse(why: str) -> HostError:
        return HostError("invalid.input", f"{why}; name the person with --peer {os.getuid()}=person:p_…")

    cfg = _config(Path(root), Files.direct())
    wid = ((cfg or {}).get("workspace") or {}).get("id")
    if not isinstance(wid, str):
        raise refuse(f"{root} is not an orch workspace")
    from orch.custody.base import KeyNotFound
    from orch.custody.select import device_backend

    try:  # either custody (passphrase key file or convenience selection); only the public key is read
        backend, _sel = device_backend(state, wid)
        if not backend.exists(human.KEY_ID):  # the selection is metadata: the key itself must still be there
            raise KeyNotFound(human.KEY_ID)
        device = device_ref(backend.public_key(human.KEY_ID))
    except KeyNotFound:
        raise refuse("no device key of yours for this workspace on this machine") from None
    except Exception as exc:  # noqa: BLE001 - an unreadable key or selection: ask for the mapping instead
        raise refuse(f"the device key could not be read ({exc})") from None
    pins = HostPins(state, wid)
    try:
        genesis = pins.genesis()
    except StoreError as exc:
        raise refuse(f"no genesis pin ({exc.code})") from None
    finally:
        pins.close()
    try:
        store = Store.open(root, expected_workspace_id=wid, expected_genesis=genesis, host_state_dir=None, load="lazy")
    except StoreError as exc:
        raise refuse(f"the workspace could not be read ({exc.code})") from None
    try:
        ws = store.state.workspace
        found = ws.devices.get(device)
        person = getattr(found, "person", None)
        dead = found is not None and _dead_device(found, person, ws)
    finally:
        store.close()
    if not person:
        raise refuse(f"device {device} is not a device of this workspace")
    if dead:  # the key on this machine is the evidence for the default: a dead key is none (H7 review)
        raise refuse(f"the device key on this machine ({device}) is {dead}")
    return {str(os.getuid()): {"class": "person", "person": person}}


def _dead_device(device: Any, person: str | None, ws: Any) -> str | None:
    """Why a device cannot stand for its person (``None``: it can): removed, revoked, expired, without ``decide``, or
    its person is no longer a member."""
    import time

    from orch.identity.certs import person_scope_ok

    if device.removed:
        return "removed"
    if device.revoked:
        return "revoked"
    if device.expires_ms is not None and device.expires_ms <= time.time() * 1000:
        return "expired"
    if not person_scope_ok({"scopes_max": list(device.scopes)}):
        return "without the decide scope"
    if person not in ws.members:
        return "of a person who is no longer a member"
    return None


_clock = time.time  # the clock of request expiry for `convenience on` (tests set it to the lab's clock)


def _prefix_of(entries: list[dict[str, Any]], workspace_id: str | None) -> str | None:
    """The registered prefix of a workspace id (for a hint), else None."""
    for item in entries:
        prefix = item.get("prefix")
        if workspace_id is not None and item.get("workspace_id") == workspace_id and isinstance(prefix, str):
            return prefix
    return None


def _cwd_workspace(entries: list[dict[str, Any]]) -> str | None:
    """The workspace id whose registered root is the current directory or its nearest registered parent, else None.
    Fail closed: None when the logical ``$PWD`` is not the physical directory (a symlink could point this process at
    another workspace), and registered roots that are empty or not absolute never match."""
    try:
        here = os.getcwd()
    except OSError:
        return None
    logical = os.environ.get("PWD")
    if logical and logical != here:
        return None
    best: tuple[int, str] | None = None
    for item in entries:
        raw = item.get("root")
        if not isinstance(raw, str) or not raw.startswith("/"):
            continue
        try:
            root = Path(raw).resolve()
        except (OSError, RuntimeError):
            continue
        if root == Path(here) or root in Path(here).parents:
            if best is None or len(root.parts) > best[0]:
                best = (len(root.parts), str(item.get("workspace_id")))
    return best[1] if best else None


def _convenience(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    """``orch host convenience on|off|show [--workspace W]``: the per-workspace click-to-sign flag in the host state dir
    (host UID only). Switching it on is an operator decision: every reader of the workspace must know auth
    convenience (orch-v2-host §0.8.6 item 4); the host still checks the readers condition at every prepare. Errors use
    the CLI's catalog envelope."""
    from orch.custody.cli import write_error

    from . import convenience
    from .registry import registry_path

    state = _state(args.state, env)
    uid = os.getuid()
    if env.get("ORCH_GRANT"):
        return write_error(out, False, "human_only", "an agent never switches signing custody")
    if args.switch == "show":
        out.write(json.dumps(convenience.read(state, uid), sort_keys=True) + "\n")
        return 0
    from .custody import read_json

    reg = read_json(registry_path(state), uid) or {"workspaces": []}
    here = None
    if not args.workspace:
        # only `off` (it takes authority away) may default to the folder's workspace; `on` always names it
        found = _cwd_workspace(reg.get("workspaces") or [])
        here = found if args.switch == "off" else None
        if here is None:
            shown = _prefix_of(reg.get("workspaces") or [], found) or "<your workspace prefix>"
            return write_error(
                out,
                False,
                "invalid.input",
                "--workspace is required"
                + (" (or run `off` inside the workspace folder)" if args.switch == "off" else ""),
                hint=f"orch host convenience {args.switch} --workspace {shown}",
                fix=["orch", "host", "convenience", args.switch, "--workspace", shown],
            )
        args.workspace = here
    match = [w for w in reg.get("workspaces") or [] if args.workspace in (w.get("workspace_id"), w.get("prefix"))]
    if len(match) != 1:
        return write_error(out, False, "not_found", f"no single registered workspace {args.workspace}")
    wid = match[0]["workspace_id"]
    if here is not None:  # named before acting, so a wrong folder is seen
        out.write(f"this folder belongs to {match[0].get('prefix')} ({wid}): switching click-to-sign off for it\n")
    try:
        if (
            args.switch == "on"
        ):  # B10: before the lock (like custody_on): never leave the flag on with every click refused
            convenience.refuse_turn_on(state, match[0], uid)
        if args.switch == "off":
            # V3-D31: off also moves a key in convenience custody back under a passphrase (typed on /dev/tty by the key
            # backend); no terminal: the flag only, with the hint below
            from orch.custody import cli as custody_cli
            from orch.custody import select
            from orch.custody.base import CustodyError

            try:
                # flag off (+ sweep) under the lock; the /dev/tty prompt of the move runs without it (L2-1075-4)
                select.turn_off_convenience(state, wid, uid, move_key=False)
                moved = None
                st = select.status(state, wid)
                if custody_cli.has_terminal() and (st["custody"] == "convenience" or "problem" in st):
                    moved = select.downgrade_in_place(state, wid)
            except CustodyError as exc:
                return write_error(
                    out,
                    False,
                    "custody.unavailable",
                    f"click-to-sign is off, but the key could not be moved back: {exc}",
                    hint=f"orch keys custody passphrase --workspace {match[0].get('prefix') or wid} --in-place",
                )
        with convenience.workspace_lock(state, wid, uid) if args.switch == "on" else contextlib.nullcontext():
            if args.switch == "on":
                waiting = convenience.pending_handoffs(state, wid, uid, clock=_clock)
                if waiting:
                    lines = ", ".join(f"{r['id']} ({r['op']}, code {r['code']})" for r in waiting)
                    return write_error(
                        out,
                        False,
                        "sign.click_only",
                        f"{len(waiting)} terminal hand-off request(s) are pending for this workspace: {lines}",
                        hint="let them expire (10 minutes), then switch convenience on",
                    )
            if args.switch == "on":
                convenience.set_enabled(state, wid, True, uid)
    except HostError as exc:  # lock.busy (a lock is held), custody.unsafe_mode (a lock or flag file is not private)
        return write_error(out, False, exc.code, exc.message, hint=exc.hint)
    word = "on" if args.switch == "on" else "off"
    if args.switch == "off" and convenience.enabled(state, wid, uid):  # never claim "off" for a flag still on
        return write_error(
            out, False, "custody.unavailable", "the click-to-sign flag is still on; run the command again"
        )
    out.write(f"click-to-sign is {word} for {match[0].get('prefix')} ({wid})\n")
    if args.switch == "off":
        from orch.custody import select  # the same custody reading `orch sign` resolves its key from

        held = select.status(state, wid).get("custody")
        if moved is not None:
            out.write("The key is back under your passphrase: sign in the Terminal with orch sign <code>\n")
        elif held == "convenience":
            out.write(
                "The key stays in one-click custody, so it cannot sign in the Terminal yet. To sign with a passphrase: "
                f"orch keys custody passphrase --workspace {match[0].get('prefix') or wid} --in-place\n"
            )
        elif held == "passphrase":
            out.write("Sign in the Terminal with orch sign <code>\n")
    if args.switch == "on":
        out.write(
            "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.\n"
        )
        out.write(
            "Every other reader (another device, another member's core, a relay link) must declare reader level 1 "
            "first: until then each convenience signature is refused and names it (orch keys reader declare).\n"
        )
    return 0


def _readers(args: argparse.Namespace, env: Mapping[str, str], out: TextIO) -> int:
    """``orch host readers reset --workspace W`` (H12d-3, §0.8.6 item 9.10 "A known state restore"): drop every reader
    record of the workspace. Fail closed: every device then counts as level 0 (convenience signing is refused while
    another reader is below level 1, phones get ``declaration_unknown`` and re-declare on their own, cores re-declare
    with ``orch keys reader declare``). A person's decision, never an agent's (``human_only`` under ``ORCH_GRANT``);
    it works with the host up or down (removing a record only lowers a level)."""
    from orch.custody.cli import write_error
    from orch.store.readerrecs import reset_records

    from .custody import read_json
    from .registry import registry_path

    state = _state(args.state, env)
    uid = os.getuid()
    if env.get("ORCH_GRANT"):
        return write_error(out, False, "human_only", "an agent never resets reader records")
    try:
        reg = read_json(registry_path(state), uid) or {"workspaces": []}
    except HostError as exc:
        return write_error(out, False, exc.code, exc.message)
    match = [w for w in reg.get("workspaces") or [] if args.workspace in (w.get("workspace_id"), w.get("prefix"))]
    if len(match) != 1:
        return write_error(out, False, "not_found", f"no single registered workspace {args.workspace}")
    wid = match[0]["workspace_id"]
    try:
        dropped = reset_records(state, wid)
    except OSError as exc:
        return write_error(out, False, "custody.unsafe_mode", f"the reader records could not be removed ({exc})")
    data = {"workspace_id": wid, "dropped": ["d_" + d for d in dropped]}
    out.write(json.dumps({"ok": True, "data": data}, sort_keys=True) + "\n")
    return 0
