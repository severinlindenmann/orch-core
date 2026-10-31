"""``orch app``: share an app, a page or a form that an agent built, by link (the Publish add-on, #1455).

    orch app prepare REF FILE... --name N --kind page|app|form --access public|company|group [--audience A]
                                 [--reach ORIGIN]... [--slug S] [--app-id ID]   copy artifacts aside; nothing leaves
    orch app release STAGED                                                  a person: sign, then upload
    orch app send    STAGED                                                  a person: upload a release already signed
    orch app list    [REF]                                                   staged releases and apps
    orch app discard STAGED                                                  drop a staged release

An agent (``ORCH_GRANT`` set) may ``prepare``, ``list`` and ``discard`` on its own ticket and nothing else: ``release``
and ``send`` are refused here (``op.not_allowed``) and again by the host (column P only). Every release needs a person's
signature over the exact name, access, reach and files. The upload is not the same as going live: the server serves a
version only after the owner's approval of its commit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

from .publish import PubError, _call, _sign, _target, _tty_ok

__all__ = ["main"]

_USAGE = (
    "usage: orch app {prepare REF FILE... --name N --kind page|app|form --access public|company|group "
    "[--audience A] [--reach ORIGIN]... [--slug S] [--app-id ID] | release STAGED | send STAGED | list [REF] | "
    "discard STAGED} [--json]"
)
_AGENT_OK = frozenset({"prepare", "list", "discard"})


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch app", add_help=False)
    p.add_argument("cmd", choices=("prepare", "release", "send", "list", "discard"))
    p.add_argument("rest", nargs="*")
    p.add_argument("--name")
    p.add_argument("--kind", choices=("page", "app", "form"))
    p.add_argument("--access", choices=("public", "company", "group"))
    p.add_argument("--audience")
    p.add_argument("--reach", action="append", default=[])
    p.add_argument("--slug")
    p.add_argument("--app-id", dest="app_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workspace")
    return p


def _check_shape(a: argparse.Namespace) -> None:
    want = {"prepare": (2, 10**6), "release": (1, 1), "send": (1, 1), "discard": (1, 1), "list": (0, 1)}[a.cmd]
    if not want[0] <= len(a.rest) <= want[1]:
        raise PubError("invalid.input", _USAGE, 2)
    if a.cmd == "prepare" and not (a.name and a.kind and a.access):
        raise PubError("invalid.input", "prepare needs --name, --kind and --access\n" + _USAGE, 2)


def _send(target: Any, staged: str, env: Mapping[str, str], out: TextIO, as_json: bool) -> int:
    got = _call(target, "app.send", {"staged_id": staged}, env)
    if as_json:
        out.write(json.dumps({"ok": True, "data": got}) + "\n")
    else:
        out.write(f"ok app uploaded {got.get('app_id')} version {got.get('version')}: not live yet\n")
        out.write(f"{got.get('needs')}\n")
    return 0


def _list_text(data: Mapping[str, Any]) -> str:
    lines = []
    for s in data.get("staged") or []:
        who = "an agent" if s.get("origin") == "agent" else "you"
        lines.append(
            f"staged {s['staged_id']} {s['ticket']} {s['name']} ({s['kind']}, {s['access']}) version {s['version']} "
            f"{s['files']} file(s) by {who}: orch app release {s['staged_id']}"
        )
    for r in data.get("apps") or []:
        lines.append(
            f"app {r['id']} {r['ticket']} {r['name']} ({r['kind']}, {r['access']}) uploaded {r.get('uploaded')}"
        )
    return "\n".join(lines) if lines else "nothing staged or shared"


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
            raise PubError("unsupported.platform", "orch app needs POSIX filesystem semantics")
        a = _parser().parse_intermixed_args(argv)
        if env.get("ORCH_GRANT") and a.cmd not in _AGENT_OK:
            raise PubError("op.not_allowed", "a person does this; an agent only prepares (orch app prepare)")
        _check_shape(a)
        target = _target(env)
        if a.cmd == "prepare":
            args: dict[str, Any] = {
                "ref": a.rest[0], "artifacts": a.rest[1:], "name": a.name, "kind": a.kind, "access": a.access,
            }  # fmt: skip
            for key in ("audience", "slug", "app_id"):
                if getattr(a, key):
                    args[key] = getattr(a, key)
            if a.reach:
                args["reach"] = a.reach
            data = _call(target, "app.prepare", args, env)
            if as_json:
                out.write(json.dumps({"ok": True, "data": data}) + "\n")
                return 0
            out.write(
                f"ok app staged {data['staged_id']} ({len(data['files'])} file(s), {data['access']}) for "
                f"{data['ticket']}\nnothing has left this computer and nothing is live. next: a person signs it: "
                f"orch app release {data['staged_id']}\n"
            )
            return 0
        if a.cmd == "discard":
            _call(target, "app.discard", {"staged_id": a.rest[0]}, env)
            out.write(f"ok app discarded {a.rest[0]}\n")
            return 0
        if a.cmd == "release":
            if not _tty_ok():
                raise PubError("tty.required", "no terminal to sign on: run this in a terminal; nothing was signed", 3)
            _sign(target, "app.release", {"staged_id": a.rest[0]})
            try:
                return _send(target, a.rest[0], env, out, as_json)
            except (PubError, OrchError):
                out.write(f"your signature stays good for an hour: orch app send {a.rest[0]}\n")
                raise
        if a.cmd == "send":
            return _send(target, a.rest[0], env, out, as_json)
        data = _call(target, "app.list", {"ref": a.rest[0]} if a.rest else {}, env)
        out.write((json.dumps({"ok": True, "data": data}) if as_json else _list_text(data)) + "\n")
        return 0
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))
