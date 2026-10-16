"""``orch keys``: custody of this person's device key per workspace (orch-v2-host §0.8.6 item 3). Terminal only.

    orch keys custody status [--workspace W]
    orch keys custody convenience --workspace W        # one passphrase prompt; needs the workspace's flag on
    orch keys custody passphrase --workspace W --in-place
    orch keys forget --workspace W                     # removes local convenience custody (not revocation)
    orch keys primary init                             # the person-level primary key pair (passphrase custody only)
    orch keys primary status                           # its public facts: device id, public keys, purposes
    orch keys reader declare --workspace W             # H12d-2: declare this machine's device a reader (one prompt)
    orch keys reader declare --enrol --workspace-id ID --cert PATH --out PATH   # an invitee's declaration

``ORCH_GRANT`` refuses every subcommand but the two ``status`` ones, and the changing subcommands also need the
person's terminal (``/dev/tty``): they prevent accidental use through supported agent workflows, they do not
authenticate a person against a same-UID process. Errors use the CLI's catalog envelope (``{ok:false,
error:{code, message, hint?, fix?, retryable}}``), with the host's exit codes for host codes.
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
    rd = sub.add_parser("reader")
    rd.add_argument("action", choices=("declare",))
    rd.add_argument("--workspace")
    rd.add_argument("--enrol", action="store_true")
    rd.add_argument("--workspace-id")
    rd.add_argument("--cert", type=Path)
    rd.add_argument("--out", type=Path)
    rd.add_argument("--state", type=Path)
    rd.add_argument("--json", action="store_true")
    fg = sub.add_parser("forget")
    fg.add_argument("--workspace", required=True)
    fg.add_argument("--state", type=Path)
    fg.add_argument("--json", action="store_true")
    pr = sub.add_parser("primary")
    pr.add_argument("action", choices=("init", "status"))
    pr.add_argument("--state", type=Path)
    pr.add_argument("--json", action="store_true")
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


def has_terminal() -> bool:
    """A controlling terminal (``/dev/tty``) can be opened. Tests replace this."""
    from orch.ops.caps import open_tty

    try:
        fd = open_tty(os.O_RDWR)
    except OSError:
        return False
    try:
        return os.isatty(fd)
    finally:
        os.close(fd)


def _need_terminal(env: Mapping[str, str]) -> None:
    if env.get("ORCH_GRANT"):
        raise _Fail("human_only", "orch keys is a person's command; an agent never changes key custody")
    if not has_terminal():
        raise _Fail("custody.no_prompt", "orch keys changes custody only at your own terminal (/dev/tty)")


def _enabled(state: Path, entry: dict[str, Any]) -> bool:
    from orch.host.convenience import enabled

    return enabled(state, entry["workspace_id"], int(entry["host_uid"]))


def run(args: argparse.Namespace, env: Mapping[str, str], out: TextIO, err: TextIO) -> Any:
    from orch.custody import select
    from orch.ops.runtime import state_dir

    state = args.state.resolve() if args.state is not None else state_dir(env)
    if args.cmd == "primary":
        return _primary(args, state, env, err)
    if args.cmd == "reader":
        return _reader_declare(args, state, env)
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
        from orch.devsandbox import click_refusal

        if click_refusal(state, env) is not None:  # §11A.17.2 F3, D2 (the core function checks again)
            raise _Fail(
                "sign.convenience_unavailable",
                "dev_host: this is a Simulator dev host or a dev-pair sandbox; convenience custody is never used here",
            )
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


PRIMARY_NEXT = (
    "the primary's certificate and its relay registration are not made yet "
    "(orch-v2-host 11A.15.10 items 10 and 11); `orch keys primary status` shows them once they exist"
)


def _primary(args: argparse.Namespace, state: Path, env: Mapping[str, str], err: TextIO) -> Any:
    """``orch keys primary`` (orch-v2-host 11A.15.9): ``init`` makes the person-level primary key pair in passphrase
    custody (never convenience); ``status`` shows public facts only and never prompts."""
    from orch.custody import primary

    if args.action == "status":
        return primary.status(state)
    _need_terminal(env)
    primary.init(state)
    err.write(PRIMARY_NEXT + "\n")
    return primary.status(state)


#: bytes of a device certificate file (``--cert``), refused before it is parsed
CERT_MAX = 16 * 1024


def _reader_declare(args: argparse.Namespace, state: Path, env: Mapping[str, str]) -> dict[str, Any]:
    """``orch keys reader declare`` (§0.8.6 item 9.3, H12d-2): signed with the device key after the prompt shows every
    field; a convenience device key refuses (no review)."""
    import re
    import time

    from orch import canon
    from orch.custody import select
    from orch.host.control_client import new_idem

    from . import readerdecl

    _need_terminal(env)
    now_ms = int(time.time() * 1000)
    if args.enrol:
        wid = args.workspace_id or ""
        if not re.fullmatch(r"[0-9a-f]{32}", wid) or args.cert is None or args.out is None:
            raise _Fail("invalid.input", "--enrol needs --workspace-id (32 hex), --cert PATH and --out PATH", 5)
        try:
            with open(args.cert, "rb") as fh:
                raw = fh.read(CERT_MAX + 1)
            if len(raw) > CERT_MAX:
                raise _Fail("invalid.input", f"--cert is larger than {CERT_MAX} bytes", 5)
            cert = canon.loads_strict(raw)
        except (OSError, canon.JcsError, ValueError) as exc:
            raise _Fail("parse.json", f"--cert is not a device certificate ({exc})", 5) from None
        backend, _ = select.device_backend(state, wid)
        try:
            decl = readerdecl.enrol(backend, wid, cert, now_ms)
        except ValueError as exc:
            raise _Fail("invalid.input", str(exc), 5) from None
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(canon.cj_checked(decl) + b"\n")
        return {"workspace_id": wid, "reader": decl["o"]["reader"], "out": str(args.out)}
    entry = _one(state, args.workspace)
    wid = entry["workspace_id"]
    backend, _ = select.device_backend(state, wid)
    from orch.host.client import Connection, Pin
    from orch.hostclient import workspace_socket

    path = workspace_socket(env, state, int(entry["host_uid"]), wid)
    try:
        conn = Connection(Pin(int(entry["host_uid"]), wid, path), client="orch keys")
    except (OSError, ConnectionError) as exc:
        raise _Fail("host.unavailable", f"the workspace is not served ({exc}); start the host first", 9) from None
    try:
        try:
            answer = readerdecl.declare_hosted(conn, backend, wid, now_ms, new_idem())
        except LookupError as exc:
            raise _Fail("not_found", str(exc), 2) from None
    finally:
        conn.close()
    return {"workspace_id": wid, **answer}


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


def envelope(code: str, message: str, *, hint: str | None = None) -> tuple[dict[str, Any], int]:
    """The catalog error envelope and exit code (the P1 catalog, then the host's table)."""
    from orch.cli.render import error_envelope
    from orch.host.errors import disposition
    from orch.ops.errors import OrchError

    env = error_envelope(OrchError(code, message[:400], hint=hint))
    exit_code, retryable = disposition(code)
    env["error"]["retryable"] = bool(env["error"].get("retryable") or retryable)
    return env, exit_code


def write_error(out: TextIO, as_json: bool, code: str, message: str, *, hint: str | None = None) -> int:
    from orch.cli.render import error_text

    env, exit_code = envelope(code, message, hint=hint)
    out.write((json.dumps(env, sort_keys=True) if as_json else error_text(env)) + "\n")
    return exit_code


def _fail(out: TextIO, as_json: bool, code: str, message: str, exit_code: int) -> int:
    return write_error(out, as_json, code, message)
