"""``python -I -m orch.records``: the one-shot person-side records tool (H11e).

    python -I -m orch.records init           [--workspace ID] [--json]
    python -I -m orch.records commit         [--workspace ID] [--fresh-history] [--json]
    python -I -m orch.records push           [--workspace ID] [--remote NAME] [--initial] [--json]
    python -I -m orch.records remote-private [--workspace ID] [--remote NAME] [--off] [--yes] [--json]

It talks to the host that serves the workspace (``records.snapshot``, ``records.export_check``,
``records.remote_private``; column P, owner or maintainer) and does the git work itself as the person. A workspace the
host does not serve is refused for now (direct mode, D-P2-38, is a later item); so is a call under ``ORCH_GRANT``
(agents never commit or push records through orch). Windows is refused (``records.unsupported``) until
reparse-point-safe resolution exists.

``remote-private`` marks the **one exact effective push URL** of the remote (as git resolves it now: ``pushurl``,
``insteadOf``) as read only by the owner. orch trusts that word; a later change of the destination resolves to another
URL and is not marked. Without the mark, a workspace with convenience signing (the flag on, or convenience events in
its history) cannot push records (``sign.convenience_readers``).

Output: one line of text, or with ``--json`` one JSON object (``{"ok": true, ...}`` or ``{"ok": false, "error":
{code, message, hint?}}``). Exit codes follow :class:`orch.records.RecordsError` and the host's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import RecordsError
from .host_link import TRUST_PROMPT, resolve
from .tool import Link, ValidatedTips, commit, init_repo, push, push_url

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -I -m orch.records", description="commit and push orch records")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "commit", "push", "remote-private"):
        s = sub.add_parser(name)
        s.add_argument("--workspace", help="the workspace id (default: the workspace here)")
        s.add_argument("--json", action="store_true")
        if name == "commit":
            s.add_argument("--fresh-history", action="store_true", help="make the next commit a root commit")
        if name in ("push", "remote-private"):
            s.add_argument("--remote", default="origin")
        if name == "push":
            s.add_argument("--initial", action="store_true", help="create the remote branch (terminal only)")
        if name == "remote-private":
            s.add_argument("--off", action="store_true", help="remove the mark")
            s.add_argument("--yes", action="store_true", help="do not ask (you have read the warning)")
    return p


def _confirm(out: TextIO, text: str) -> bool:
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(text + "\nType yes to continue: ")
            tty.flush()
            return tty.readline().strip() == "yes"
    except OSError:
        out.write("no terminal to confirm on: pass --yes after reading the warning\n")
        return False


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    *,
    link: Link | None = None,
    root: Path | None = None,
    workspace_id: str | None = None,
    tips: ValidatedTips | None = None,
    protocols: str | None = None,
) -> int:
    """The CLI. ``link``, ``root``, ``workspace_id``, ``tips`` and ``protocols`` are seams for tests (an in-process
    host connection and local bare repos); production resolves them from the person's registry."""
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    args = _parser().parse_args(argv)

    def emit(ok: bool, data: dict[str, Any], text: str) -> None:
        if args.json:
            out.write(json.dumps({"ok": ok, **data}, sort_keys=True) + "\n")
        else:
            out.write(text + "\n")

    try:
        if os.name == "nt":
            raise RecordsError("records.unsupported", "the records tool does not run on Windows yet (D-P2-38)")
        if env.get("ORCH_GRANT"):
            raise RecordsError("op.not_allowed", "agents do not commit or push records through orch", exit_=3)
        if link is None or root is None or workspace_id is None:
            r = resolve(env, args.workspace)
            link, root, workspace_id = link or r.link, root or r.root, workspace_id or r.workspace_id
            tips = tips or r.tips
        from .git import ALLOWED_PROTOCOLS, open_repo

        protos = protocols or ALLOWED_PROTOCOLS
        if args.cmd == "init":
            res = init_repo(root, env=env)
            open_repo(root, env=env, protocols=protos)  # the result must be a usable dedicated repo
            emit(True, res, "records repo ready" + (" (created)" if res["created"] else ""))
            return 0
        if args.cmd == "commit":
            repo = open_repo(root, env=env, protocols=protos)
            res = commit(repo, link, workspace_id, fresh=args.fresh_history)
            text = (
                f"committed {res['commit'][:12]} (ws_seq {res['ws_seq']}, index {res['index']})"
                if res["changed"]
                else f"nothing to commit ({res['commit'][:12]})"
            )
            emit(True, res, text)
            return 0
        if args.cmd == "push":
            repo = open_repo(root, env=env, protocols=protos, remote=args.remote)
            assert tips is not None
            res = push(repo, link, workspace_id, tips, remote=args.remote, initial=args.initial)
            emit(True, res, f"pushed {res['commit'][:12]} to {res['url']}" if res["pushed"] else "up to date")
            return 0
        # remote-private
        if args.off:
            res = link("records.remote_private", {"on": False})
            emit(True, res, "private-remote mark removed")
            return 0
        repo = open_repo(root, env=env, protocols=protos, remote=args.remote)
        url = push_url(repo, args.remote)
        prompt = TRUST_PROMPT.format(url=url)
        if not args.yes and not _confirm(out, prompt):
            raise RecordsError("records.unconfirmed", "not confirmed; nothing changed", exit_=3)
        res = link("records.remote_private", {"url": url, "on": True})
        emit(True, res, f"marked {url} private: {res.get('text', '')}")
        return 0
    except RecordsError as exc:
        err = {"code": exc.code, "message": exc.message, **({"hint": exc.hint} if exc.hint else {})}
        emit(False, {"error": err}, f"{exc.code}: {exc.message}" + (f"\n  {exc.hint}" if exc.hint else ""))
        return exc.exit
    except Exception as exc:  # noqa: BLE001 - a host refusal (HostFailure) or the like: report it, never a traceback
        code = getattr(exc, "code", "internal")
        message = getattr(exc, "message", str(exc))
        hint = getattr(exc, "hint", None)
        err = {"code": code, "message": message, **({"hint": hint} if hint else {})}
        emit(False, {"error": err}, f"{code}: {message}" + (f"\n  {hint}" if hint else ""))
        return int(getattr(exc, "wire_exit", None) or getattr(exc, "exit", None) or 1)
