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
from collections.abc import Mapping
from pathlib import Path
from typing import TextIO

from .errors import HostError

__all__ = ["main"]


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
    reg = sub.add_parser("register")
    reg.add_argument("root")
    reg.add_argument("--peer", action="append", required=True, metavar="UID=person:p_…|agent|worker")
    reg.add_argument("--state", type=Path)
    reg.add_argument("--run", type=Path)
    unreg = sub.add_parser("unregister")
    unreg.add_argument("workspace_id")
    unreg.add_argument("--state", type=Path)
    unreg.add_argument("--run", type=Path)
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


def _control_path(given: Path | None, state: Path) -> Path:
    """``--run``, else the service-managed run directory when a host serves there, else the dev ``<state>/run``."""
    from .listener import default_run_directory

    if given is not None:
        return given / "control" / "host.sock"
    try:
        service = default_run_directory(os.getuid()) / "control" / "host.sock"
    except Exception:  # noqa: BLE001 - an unsupported platform has no service run directory
        service = None
    if service is not None and service.exists():
        return service
    return state / "run" / "control" / "host.sock"


def main(argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "run":
            from .listener import default_run_directory
            from .release import preload
            from .service import Host

            state = _state(args.state, env)
            run_dir = args.run or (state / "run" if args.foreground else default_run_directory(os.getuid()))
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
        from .control_client import ControlClient, new_idem
        from .ownership import LOCK_WAIT_SECONDS

        state = _state(args.state, env)
        idem = new_idem() if args.cmd in {"register", "unregister"} else None
        try:
            # A write may wait 60 s for the ownership lock: the client waits longer than the host does.
            with ControlClient(_control_path(args.run, state), timeout=LOCK_WAIT_SECONDS + 30) as client:
                if args.cmd == "list":
                    res = client.call("host.list")
                elif args.cmd == "restart":
                    res = client.call("host.restart")
                elif args.cmd == "register":
                    root = os.path.abspath(args.root)
                    res = client.call("host.register", {"root": root, "peers": _peers(args.peer)}, idem=idem)
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
        out.write(json.dumps(res, sort_keys=True) + "\n")
        return 0 if res.get("ok") else int(res.get("exit", 1))
    except HostError as exc:
        out.write(json.dumps({"ok": False, "error": exc.error()}, sort_keys=True) + "\n")
        return exc.exit
    except (OSError, ConnectionError) as exc:
        error = {"code": "host.unavailable", "message": str(exc)[:400], "retryable": True}
        out.write(json.dumps({"ok": False, "error": error}, sort_keys=True) + "\n")
        return 9
