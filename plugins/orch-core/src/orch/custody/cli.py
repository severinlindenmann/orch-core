"""``orch keys``: custody of this person's device key per workspace (orch-v2-host §0.8.6 item 3). Terminal only.

    orch keys custody status [--workspace W]
    orch keys custody convenience --workspace W        # one passphrase prompt; needs the workspace's flag on
    orch keys custody passphrase --workspace W --in-place
    orch keys forget --workspace W                     # removes local convenience custody (not revocation)

``ORCH_GRANT`` and a missing terminal refuse: they prevent accidental use through supported agent workflows, they do
not authenticate a person against a same-UID process.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

__all__ = ["main"]

RISK = "Convenience signing: agents running as you, or anything that takes over this dashboard, could sign."
DOWNGRADE_WARNING = (
    "Later signatures will say passphrase, but anyone who copied the key can still sign with any label; "
    "use --rotate for a clean break (not available yet: it needs device ops)."
)


class _Fail(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 3) -> None:
        super().__init__(message)
        self.code, self.message, self.exit = code, message, exit_code


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orch keys")
    sub = p.add_subparsers(dest="cmd", required=True)
    cus = sub.add_parser("custody")
    cus.add_argument("to", choices=("status", "convenience", "passphrase"))
    cus.add_argument("--workspace")
    cus.add_argument("--in-place", action="store_true")
    cus.add_argument("--rotate", action="store_true")
    cus.add_argument("--state", type=Path)
    cus.add_argument("--json", action="store_true")
    fg = sub.add_parser("forget")
    fg.add_argument("--workspace", required=True)
    fg.add_argument("--state", type=Path)
    fg.add_argument("--json", action="store_true")
    return p


def _entries(state: Path, pick: str | None) -> list[dict[str, Any]]:
    from orch.host.handoff import _own_workspaces

    return _own_workspaces(state, os.getuid(), pick)


def _one(state: Path, pick: str | None) -> dict[str, Any]:
    if not pick:
        raise _Fail("invalid.input", "--workspace is required", 5)
    found = _entries(state, pick)
    if len(found) != 1:
        raise _Fail("not_found", f"no single workspace {pick} in your registry maps you to a person", 2)
    return found[0]


def _need_terminal(env: Mapping[str, str]) -> None:
    if env.get("ORCH_GRANT"):
        raise _Fail("human_only", "orch keys is a person's command; an agent never changes key custody")


def _enabled(state: Path, entry: dict[str, Any]) -> bool:
    from orch.host.convenience import enabled

    return enabled(state, entry["workspace_id"], int(entry["host_uid"]))


def run(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> Any:
    from orch.custody import select
    from orch.ops.runtime import state_dir

    state = args.state.resolve() if args.state is not None else state_dir(env)
    if args.cmd == "forget":
        _need_terminal(env)
        entry = _one(state, args.workspace)
        removed = select.forget(state, entry["workspace_id"])
        err.write("forget removes this machine's copy only; it is not device revocation, and backups keep copies\n")
        return {"workspace_id": entry["workspace_id"], "removed": removed}
    if args.to == "status":
        rows = []
        for e in _entries(state, args.workspace):
            st = select.status(state, e["workspace_id"])
            rows.append(
                {"workspace_id": e["workspace_id"], "prefix": e.get("prefix"), **st, "enabled": _enabled(state, e)}
            )
        return {"workspaces": rows}
    _need_terminal(env)
    entry = _one(state, args.workspace)
    wid = entry["workspace_id"]
    if args.to == "convenience":
        if not _enabled(state, entry):
            raise _Fail(
                "sign.convenience_unavailable",
                "disabled: click-to-sign is off for this workspace (orch host convenience on --workspace W)",
            )
        err.write(RISK + "\n")
        sel = select.convert_to_convenience(state, wid, prefix=str(entry.get("prefix") or ""))
        return {"workspace_id": wid, "custody": sel["custody"], "device": sel["device"], "gen": sel.gen}
    if args.rotate:
        raise _Fail("not_implemented", "--rotate needs device ops (designed in orch-v2-host §0.8.6, not built yet)", 2)
    if not args.in_place:
        raise _Fail("invalid.input", "moving back needs --in-place (same key) or --rotate (a new device)", 5)
    err.write(DOWNGRADE_WARNING + "\n")
    sel = select.downgrade_in_place(state, wid)
    return {"workspace_id": wid, "custody": sel["custody"], "device": sel["device"], "gen": sel.gen}


def main(
    argv: list[str], *, env: Mapping[str, str] | None = None, out: TextIO | None = None, err: TextIO | None = None
) -> int:
    from orch.custody.base import CustodyError

    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        result = run(args, env, out, err)
    except _Fail as exc:
        return _fail(out, as_json, exc.code, exc.message, exc.exit)
    except CustodyError as exc:
        return _fail(out, as_json, exc.code, str(exc), 3)
    except Exception as exc:  # noqa: BLE001 - e.g. no terminal for the passphrase
        return _fail(out, as_json, getattr(exc, "code", None) or type(exc).__name__, str(exc)[:400], 3)
    if as_json:
        out.write(json.dumps({"ok": True, "result": result}, sort_keys=True) + "\n")
    else:
        out.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    return 0


def _fail(out: TextIO, as_json: bool, code: str, message: str, exit_code: int) -> int:
    if as_json:
        out.write(json.dumps({"ok": False, "error": {"code": code, "message": message}}, sort_keys=True) + "\n")
    else:
        out.write(f"orch keys: {code}: {message}\n")
    return exit_code
