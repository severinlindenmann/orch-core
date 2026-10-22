"""``orch hub``: join a hub in selective mode, and leave it (V3 C2a; hub contract 9.1, spec 8 and 8a).

    orch hub join INVITE --hub-card FILE [--hub-addr HOST:PORT] [--mode selective] [--owner-user-id HEX32]
                                                                                       a person: join (signed)
    orch hub leave                                                                       a person: leave (signed)
    orch hub connect INVITE --hub-card FILE   a person: finish a join the hub could not take yet
    orch hub unbind                           a person: tell the hub about a leave it has not heard yet
    orch hub status                           a person: the joined hub and the link state

``join`` and ``leave`` are signed with the owner's own key on the owner's terminal and are refused to agents
(``ORCH_GRANT`` set: ``op.not_allowed``; the host refuses them again). The join is **selective**: the hub sees only what
you share, never gets the workspace key, and is not a member. ``--mode full`` is refused until C2b.
``leave`` works while the hub is down: it is logged at once and the hub is told when it can be reached.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from orch.cli.publish import PubError, _call, _sign, _tty_ok

__all__ = ["main"]

_USAGE = (
    "usage: orch hub {join INVITE --hub-card FILE [--hub-addr HOST:PORT] [--mode selective] [--owner-user-id HEX32] "
    "| leave | connect INVITE --hub-card FILE [--hub-addr HOST:PORT] | unbind | status} [--json]"
)
_CARD_MAX = 64 * 1024


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch hub", add_help=False)
    p.add_argument("cmd", choices=("join", "leave", "connect", "unbind", "status"))
    p.add_argument("rest", nargs="*")
    p.add_argument("--hub-card")
    p.add_argument("--hub-addr")
    p.add_argument("--mode", default="selective")
    p.add_argument("--owner-user-id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workspace")
    return p


def _target(env: Mapping[str, str]) -> Any:
    from orch import hostclient
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace

    ws = Workspace(env, time.time, files=Files.direct())
    target = hostclient.locate(env, ws)
    if target is None:
        raise PubError("host.required", "A hub needs the orch host; this workspace is not served by one",
                       hint="run: orch host enable")  # fmt: skip
    return target


def _read_card(path: str) -> dict[str, Any]:
    """The hub card file: strict JSON (the contract's subset), at most 64 KiB; the host verifies it, not this."""
    from orch.canon import JcsError, loads_strict

    try:
        raw = Path(path).read_bytes()[: _CARD_MAX + 1]
    except OSError:
        raise PubError("invalid.input", f"cannot read the hub card file {path[:80]!r}", 5) from None
    if len(raw) > _CARD_MAX:
        raise PubError("invalid.input", "the hub card file is too large", 5)
    try:
        card = loads_strict(raw)
    except (JcsError, ValueError):
        raise PubError("invalid.input", "the hub card file is not a valid card (strict JSON)", 5) from None
    if not isinstance(card, dict):
        raise PubError("invalid.input", "the hub card file is not a hub card", 5)
    return card


def _check_shape(a: argparse.Namespace) -> None:
    n = {"join": 1, "connect": 1, "leave": 0, "unbind": 0, "status": 0}[a.cmd]
    if len(a.rest) != n:
        raise PubError("invalid.input", _USAGE, 2)
    if a.cmd in ("join", "connect") and not a.hub_card:
        raise PubError("invalid.input", _USAGE, 2)
    if a.cmd == "join" and a.mode not in ("selective", "full"):
        raise PubError("invalid.input", f"--mode {a.mode[:20]!r}: use selective", 5)
    if a.hub_addr is not None:
        from orch.hubclient.errors import HubRefused
        from orch.hubclient.tlsadapter import parse_addr

        try:
            parse_addr(a.hub_addr)
        except HubRefused:
            raise PubError("invalid.input", "--hub-addr is HOST:PORT", 5) from None
    if a.owner_user_id is not None and (a.cmd != "join" or re.fullmatch(r"[0-9a-f]{32}", a.owner_user_id) is None):
        raise PubError("invalid.input", "--owner-user-id is 32 lower-case hex digits and belongs to join", 5)


def _person(target: Any) -> str:
    entry = target.entry
    person = ((entry or {}).get("peers") or {}).get(str(os.getuid()), {}).get("person") if entry else None
    if not isinstance(person, str):
        raise PubError("human_only", "this UID is not mapped to a person in this workspace")
    return person.removeprefix("p_")


def _emit(out: TextIO, as_json: bool, data: dict[str, Any], text: str) -> None:
    out.write((json.dumps({"ok": True, "data": data}) if as_json else text) + "\n")


def _connect(target: Any, env: Mapping[str, str], a: argparse.Namespace, out: TextIO) -> int:
    from orch.ops.errors import OrchError

    try:
        args: dict[str, Any] = {"invite": a.rest[0], "hub_card": _read_card(a.hub_card)}
        if a.hub_addr:
            args["addr"] = a.hub_addr
        got = _call(target, "hub.connect", args, env)
    except OrchError as exc:
        if exc.code == "hub.unreachable":
            out.write(
                "signed: your workspace has recorded the join, but the hub could not be reached or did not answer.\n"
                f"finish it when the hub is up: orch hub connect {a.rest[0]} --hub-card {a.hub_card}"
                + (f" --hub-addr {a.hub_addr}" if a.hub_addr else "")
                + "\n"
                "or leave: orch hub leave\n"
            )
            return 9
        raise
    _emit(out, a.json, got, f"ok hub joined {got.get('hub_id')} (selective: the hub sees only what you share)")
    return 0


def _unbind(target: Any, env: Mapping[str, str], a: argparse.Namespace, out: TextIO) -> int:
    got = _call(target, "hub.unbind", {}, env)
    if not got.get("unbound"):
        _emit(out, a.json, got, "ok hub left (the hub never had a binding for this workspace)")
    elif got.get("delivered"):
        _emit(out, a.json, got, f"ok hub left {got.get('hub_id')}: the hub and publish are told")
    else:
        _emit(
            out, a.json, got,
            f"ok hub left {got.get('hub_id')}: recorded here. The hub could not be reached; "
            "tell it later with: orch hub unbind",
        )  # fmt: skip
    return 0


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
            raise PubError("unsupported.platform", "orch hub needs POSIX filesystem semantics")
        a = _parser().parse_intermixed_args(argv)
        if env.get("ORCH_GRANT") or env.get("ORCH_SESSION"):
            raise PubError("op.not_allowed", "a person does this at their own terminal; an agent never joins a hub")
        _check_shape(a)
        if a.cmd == "join" and a.mode != "selective":  # before any host or key is touched
            raise PubError("hub.full_mode_later", "a full join comes later (C2b); only --mode selective exists now", 3)
        target = _target(env)
        if a.cmd == "status":
            data = _call(target, "hub.status", {}, env)
            _emit(out, as_json, data, _status_text(data))
            return 0
        if a.cmd == "unbind":
            return _unbind(target, env, a, out)
        if a.cmd == "connect":
            return _connect(target, env, a, out)
        if not _tty_ok():
            raise PubError("invalid.input", "no terminal to sign on: run this in a terminal; nothing was signed", 5)
        if a.cmd == "join":
            args: dict[str, Any] = {"hub_card": _read_card(a.hub_card), "mode": "selective"}
            if a.owner_user_id:
                args["aliases"] = [{"person_id": _person(target), "user_id": a.owner_user_id}]
            _sign(target, "hub.join", args)
            out.write("signed: the join is recorded in your workspace log\n")
            return _connect(target, env, a, out)
        _sign(target, "hub.leave", {})
        out.write("signed: the leave is recorded in your workspace log\n")
        return _unbind(target, env, a, out)
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))


def _status_text(d: Mapping[str, Any]) -> str:
    if not d.get("hub_id"):
        return "no hub: this workspace works on its own"
    state = "joined" if d.get("joined") else "left"
    link = f", link {d['link']}" if d.get("link") else ""
    queued = ", the hub has not been told yet (orch hub unbind)" if d.get("unbound_pending") else ""
    return f"hub {d['hub_id']}: {state} ({d.get('mode')}){link}{queued}"
