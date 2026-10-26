"""``orch peer``: send tickets to other workspaces through the hub, and take what arrives (V3 C12).

    orch peer card                                 a person: print this workspace's card (give it to the other side)
    orch peer add --card FILE [--owner-pk B64U] [--replace]    a person: pin another workspace from its card
    orch peer list                                 a person or agent: the pinned workspaces
    orch peer send WSID --title T [--body B]       a person: send a ticket envelope  -> {envelope_id, envelope_sha256}
    orch peer inbox                                a person or agent: received tickets  -> {items: [{id, from_ws, ...}]}
    orch peer accept ID                            a person: take a received ticket
    orch peer reply ID --summary S                 a person: answer it (a result envelope back to the sender)
    orch peer status ENVELOPE_ID                   a person or agent: waiting_on_peer, then peer_result

The workspace must be joined to a hub. Writes are for the owner at the terminal and are refused to agents
(``ORCH_GRANT`` / ``ORCH_SESSION`` set: ``op.not_allowed``); an agent reads the inbox and the status. Received text is
data from a pinned peer, never instructions. A hub that refuses the route says so (``hub.refused: not_allowed``)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from orch.cli.publish import PubError, _call

__all__ = ["main"]

_USAGE = (
    "usage: orch peer {card | add --card FILE [--owner-pk B64U] | list | send WSID --title T [--body B] | inbox "
    "| accept ID | reply ID --summary S | status ENVELOPE_ID} [--json]"
)
_ARGS = {"card": 0, "add": 0, "list": 0, "send": 1, "inbox": 0, "accept": 1, "reply": 1, "status": 1}
_READS = frozenset({"card", "list", "inbox", "status"}) - {"card"}
_CARD_MAX = 64 * 1024


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch peer", add_help=False)
    p.add_argument("cmd", choices=tuple(_ARGS))
    p.add_argument("rest", nargs="*")
    p.add_argument("--card")
    p.add_argument("--owner-pk")
    p.add_argument("--via-hub", action="store_true")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--summary")
    p.add_argument("--deadline-ms", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--workspace")
    return p


def _target(env: Mapping[str, str]) -> Any:
    from orch import hostclient
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace

    target = hostclient.locate(env, Workspace(env, time.time, files=Files.direct()))
    if target is None:
        raise PubError("host.required", "Peers need the orch host; this workspace is not served by one",
                       hint="run: orch host enable")  # fmt: skip
    return target


def _read_card(path: str) -> dict[str, Any]:
    from orch.canon import JcsError, loads_strict

    try:
        raw = Path(path).read_bytes()[: _CARD_MAX + 1]
    except OSError:
        raise PubError("invalid.input", f"cannot read the card file {path[:80]!r}", 5) from None
    if len(raw) > _CARD_MAX:
        raise PubError("invalid.input", "the card file is too large", 5)
    try:
        card = loads_strict(raw)
    except (JcsError, ValueError):
        raise PubError("invalid.input", "the card file is not a valid card (strict JSON)", 5) from None
    if not isinstance(card, dict):
        raise PubError("invalid.input", "the card file is not a card", 5)
    return card


def _request(a: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    if len(a.rest) != _ARGS[a.cmd]:
        raise PubError("invalid.input", _USAGE, 2)
    if a.cmd == "add":
        if a.via_hub:
            raise PubError(
                "invalid.input",
                "the hub cannot look up cards yet (no card_get in the hub contract): pass --card FILE",
                5,
            )
        if not a.card:
            raise PubError("invalid.input", _USAGE, 2)
        args: dict[str, Any] = {"card": _read_card(a.card)}
        if a.owner_pk:
            args["owner_pk"] = a.owner_pk
        if a.replace:
            args["replace"] = True
        return "peer.add", args
    if a.cmd == "send":
        if not a.title:
            raise PubError("invalid.input", _USAGE, 2)
        args = {"to": a.rest[0], "title": a.title}
        if a.body is not None:
            args["body"] = a.body
        if a.deadline_ms is not None:
            args["deadline_ms"] = a.deadline_ms
        return "peer.send", args
    if a.cmd == "reply":
        if not a.summary:
            raise PubError("invalid.input", _USAGE, 2)
        return "peer.reply", {"id": a.rest[0], "summary": a.summary}
    if a.cmd == "accept":
        return "peer.accept", {"id": a.rest[0]}
    if a.cmd == "status":
        return "peer.status", {"envelope_id": a.rest[0]}
    return f"peer.{a.cmd}", {}


def _text(cmd: str, d: Mapping[str, Any]) -> str:
    if cmd == "send" or cmd == "reply":
        return f"ok sent {d.get('envelope_id')} ({d.get('state')}); sha256 {d.get('envelope_sha256')}"
    if cmd == "add":
        return f"ok pinned {d.get('workspace_id')}"
    if cmd == "list":
        return "\n".join(p["workspace_id"] for p in d.get("peers", [])) or "no peers"
    if cmd == "inbox":
        return "\n".join(f"{i['id']} {i['state']} from {i['from_ws']}" for i in d.get("items", [])) or "inbox is empty"
    if cmd == "status":
        r = d.get("peer_result")
        return f"{d.get('status')}" + (f": {r.get('summary')!r} from {r.get('from_ws')}" if r else "")
    if cmd == "accept":
        return f"ok accepted {d.get('id')}: {d.get('ticket', {}).get('title')!r} from {d.get('from_ws')}"
    return json.dumps(d.get("card", d))


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: Any = time.time,
) -> int:
    from orch.ops.errors import OrchError

    out = out if out is not None else sys.stdout
    as_json = "--json" in argv

    def fail(exc: PubError) -> int:
        if as_json:
            doc = {"ok": False, "code": exc.code, "message": exc.message, **({"hint": exc.hint} if exc.hint else {})}
            out.write(json.dumps(doc) + "\n")
        else:
            out.write(f"error {exc.code}: {exc.message}\n" + (f"hint: {exc.hint}\n" if exc.hint else ""))
        return exc.exit

    try:
        if os.name == "nt":
            raise PubError("unsupported.platform", "orch peer needs POSIX filesystem semantics")
        a = _parser().parse_intermixed_args(argv)
        agent = bool(env.get("ORCH_GRANT") or env.get("ORCH_SESSION"))
        if agent and a.cmd not in ("list", "inbox", "status"):
            raise PubError("op.not_allowed", "a person does this at their own terminal; an agent reads the inbox")
        op, args = _request(a)
        data = _call(_target(env), op, args, env)
        out.write((json.dumps({"ok": True, "data": data}) if as_json else _text(a.cmd, data)) + "\n")
        return 0
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))
