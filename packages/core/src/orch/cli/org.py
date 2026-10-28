"""``orch org``: company skills and MCP servers your hub offers to this workspace (M3 host, H-2).

    orch org list                       a person or agent: what is offered and where each item stands (names only)
    orch org sync                       a person or agent: ask the hub for its current offers (grants nothing)
    orch org show skill|mcp NAME        a person: the full text, or the exact address, sign-in and secret name
    orch org accept skill|mcp NAME      a person: turn it on, for exactly what you were shown
    orch org decline skill|mcp NAME     a person: turn it off again

Everything starts Off. ``accept`` shows the item and asks on your terminal; the host accepts only the digest it showed,
so a changed item needs a new accept. An agent can list and sync, never show, accept or decline. A company skill ranks
below orch's rules; a company server is reached through this workspace's own proxy and its results are untrusted."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

from orch.cli.publish import PubError, _call
from orch.ops.human import review_prompt

__all__ = ["main"]

_USAGE = (
    "usage: orch org {list | sync | show KIND NAME | accept KIND NAME | decline KIND NAME} [--json]"
    "  (KIND: skill | mcp)"
)
_ARGS = {"list": 0, "sync": 0, "show": 2, "accept": 2, "decline": 2}
_AGENT_OK = ("list", "sync")


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch org", add_help=False)
    p.add_argument("cmd", choices=tuple(_ARGS))
    p.add_argument("rest", nargs="*")
    p.add_argument("--json", action="store_true")
    return p


def _target(env: Mapping[str, str]) -> Any:
    from orch import hostclient
    from orch.ops.caps import Files
    from orch.ops.runtime import Workspace

    target = hostclient.locate(env, Workspace(env, time.time, files=Files.direct()))
    if target is None:
        raise PubError("host.required", "Company skills need the orch host; this workspace is not served by one",
                       hint="run: orch host enable")  # fmt: skip
    return target


def _describe(shown: dict[str, Any]) -> str:
    item, kind = shown["item"], shown["kind"]
    if kind == "skill":
        body = f"Company skill {item['name']!r}. This is the full text an agent would read:\n\n{item['text']}\n"
    else:
        sign_in = "no sign-in" if item["auth"] == "none" else f"company secret {item['secret']!r}"
        body = (
            f"Company tool server {item['name']!r}\n  address: {item['url']}\n  sign-in: {sign_in}\n"
            f"  for: {item['description'] or '(not said)'}\n  every tool starts Off.\n"
        )
    return body + "\n" + "".join(f"- {n}\n" for n in shown["notes"]) + f"  digest {item['digest'][:23]}...\n"


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

    def emit(data: dict[str, Any], text: str) -> None:
        out.write((json.dumps({"ok": True, "data": data}) if as_json else text) + "\n")

    try:
        if os.name == "nt":
            raise PubError("unsupported.platform", "orch org needs POSIX filesystem semantics")
        a = _parser().parse_intermixed_args(argv)
        if len(a.rest) != _ARGS[a.cmd] or (a.rest and a.rest[0] not in ("skill", "mcp")):
            raise PubError("invalid.input", _USAGE, 2)
        if (env.get("ORCH_GRANT") or env.get("ORCH_SESSION")) and a.cmd not in _AGENT_OK:
            raise PubError("op.not_allowed", "a person does this at their own terminal; an agent lists and syncs")
        target = _target(env)
        if a.cmd == "list":
            data = _call(target, "org.offers", {}, env)
            lines = [f"{k}: {r['name']}  [{r['status']}]" for k in ("skills", "mcp") for r in data[k]]
            emit(data, "\n".join(lines) or "nothing is offered (orch org sync)")
        elif a.cmd == "sync":
            data = _call(target, "org.sync", {}, env)
            emit(data, "synced: " + ", ".join(f"{k} {v}" for k, v in data["result"].items()))
        elif a.cmd == "show":
            data = _call(target, "org.show", {"kind": a.rest[0], "name": a.rest[1]}, env)
            emit(data, _describe(data) + f"status: {data['status']}")
        elif a.cmd == "decline":
            data = _call(target, "org.decline", {"kind": a.rest[0], "name": a.rest[1]}, env)
            emit(data, f"ok {a.rest[1]} is Off")
        else:  # accept: always on the owner's terminal, for exactly the digest that was shown
            shown = _call(target, "org.show", {"kind": a.rest[0], "name": a.rest[1]}, env)
            if shown.get("needs_isolation"):
                raise PubError("invalid.input", f"{a.rest[1]}: {shown['needs_isolation']}", 5)
            if not review_prompt(_describe(shown) + "Turn this on for this workspace?"):
                raise PubError("sign.cancelled", "not accepted; nothing changed", 3)
            data = _call(
                target, "org.accept", {"kind": a.rest[0], "name": a.rest[1], "digest": shown["item"]["digest"]}, env
            )
            emit(data, f"ok {a.rest[1]} is On (new runs only; what a run already read stays read)")
        return 0
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))
