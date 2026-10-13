"""``orch host …``: run the host, install a release, print unit files, and the control-socket commands.

``orch host run --foreground`` is the dev and test entry (run directory ``<state>/run``); the service manager runs
``orch-host run`` from a release (run directory ``/Users/Shared/orch/run/<uid>`` or ``/run/orch/<uid>``). A CLI call
never starts a host as a side effect (D-P2-2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from .errors import HostError

__all__ = ["default_peers", "enable", "ensure_service", "main"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch host", description="the orch host service")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="serve every registered workspace (the service manager runs this)")
    run.add_argument("--foreground", action="store_true", help="dev mode: run directory <state>/run")
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
    en = sub.add_parser("enable", help="install the release and the per-user service unit, start the host (tier A)")
    en.add_argument("--state", type=Path)
    en.add_argument("--run", type=Path)
    en.add_argument(
        "--replace",
        action="store_true",
        help="replace a service installed for another state dir, run dir or interpreter (its host stops)",
    )
    en.add_argument("--dev", action="store_true", help="allow an interpreter or orch package from a source checkout")
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
            preload()
            asyncio.run(Host(state, run_dir).run())
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
        if args.cmd == "enable":
            doc = enable(_state(args.state, env), env, run=args.run, replace=args.replace, dev=args.dev)
            out.write(json.dumps(doc, sort_keys=True) + "\n")
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
        out.write(json.dumps(res, sort_keys=True) + "\n")
        return 0 if res.get("ok") else int(res.get("exit", 1))
    except HostError as exc:
        out.write(json.dumps({"ok": False, "error": exc.error()}, sort_keys=True) + "\n")
        return exc.exit
    except (OSError, ConnectionError) as exc:
        error = {"code": "host.unavailable", "message": str(exc)[:400], "retryable": True}
        out.write(json.dumps({"ok": False, "error": error}, sort_keys=True) + "\n")
        return 9


def _failure(code: str, message: str, exit_: int = 9, **extra: Any) -> dict[str, Any]:
    retryable = code == "host.unavailable"
    return {
        "ok": False,
        "exit": exit_,
        "error": {"code": code, "message": message[:400], "retryable": retryable},
        **extra,
    }


def enable(
    state: Path, env: Mapping[str, str], *, run: Path | None = None, replace: bool = False, dev: bool = False
) -> dict[str, Any]:
    """``orch host enable``: install the release (D-P2-4), write the per-user unit, start it and wait for the control
    socket (H7, tier A). Windows and other platforms without a per-user service answer ``mode: direct``. Refused before
    anything is written: a run dir whose socket paths would be too long, a control character in a unit value, dev code
    as the service (unless ``dev``), and a unit installed for another state dir, run dir or interpreter (unless
    ``replace``)."""
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
    return {"ok": True, "result": {"data": data}}


def _loaded(manager: Any) -> bool:
    try:
        return bool(manager.loaded())
    except OSError:
        return False


#: the service manager of a plan (tests replace it with a fake that never runs launchctl or systemctl)
def SERVICE_MANAGER(unit: Any) -> Any:  # noqa: N802 - a module-level seam, named like the class it builds
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
    doc = enable(state, env, run=run, dev=dev)
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
    directory = human.keys_dir(state, wid)
    if not (directory / human.KEY_FILE).is_file():
        raise refuse("no device key of yours for this workspace on this machine")
    try:
        device = device_ref(human.open_backend(directory).public_key(human.KEY_ID))
    except Exception as exc:  # noqa: BLE001 - an unreadable key file: ask for the mapping instead
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
    if not args.workspace:
        return write_error(out, False, "invalid.input", "--workspace is required")
    from .custody import read_json

    reg = read_json(registry_path(state), uid) or {"workspaces": []}
    match = [w for w in reg.get("workspaces") or [] if args.workspace in (w.get("workspace_id"), w.get("prefix"))]
    if len(match) != 1:
        return write_error(out, False, "not_found", f"no single registered workspace {args.workspace}")
    wid = match[0]["workspace_id"]
    try:
        with convenience.workspace_lock(state, wid, uid):  # against a hand-off prepare in the host (§0.8.6 item 8)
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
            convenience.set_enabled(state, wid, args.switch == "on", uid)
    except HostError as exc:  # host.busy (a lock is held), custody.unsafe_mode (a lock or flag file is not private)
        return write_error(out, False, exc.code, exc.message, hint=exc.hint)
    word = "on" if args.switch == "on" else "off"
    out.write(f"click-to-sign is {word} for {match[0].get('prefix')} ({wid})\n")
    if args.switch == "on":
        out.write(
            "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign.\n"
        )
    return 0
