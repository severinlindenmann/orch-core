"""``orch org``: company skills and MCP servers your hub offers to this workspace (M3 host, H-2).

    orch org list                       a person or agent: what is offered and where each item stands (names only)
    orch org sync                       a person or agent: ask the hub for its current offers (grants nothing)
    orch org show skill|mcp NAME        a person: the full text, or the exact address, sign-in and secret name
    orch org accept skill|mcp NAME      a person: turn it on, for exactly what you were shown
    orch org decline skill|mcp NAME     a person: turn it off again
    orch org tool SERVER TOOL on|off    a person: turn one tool of an accepted company server On or Off

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
from orch.host.mcpshared import plain
from orch.ops.human import review_prompt

__all__ = ["main"]

_USAGE = (
    "usage: orch org {list | sync | show KIND NAME | accept KIND NAME | decline KIND NAME | tool SERVER TOOL on|off}"
    " [--json]"
    "  (KIND: skill | mcp)"
)
_ARGS = {"list": 0, "sync": 0, "show": 2, "accept": 2, "decline": 2, "tool": 3}
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


def _block(text: str) -> str:
    """A server-controlled, possibly multi-line text: every continuation line is indented under a fixed marker, so it
    can never look like a line of the review itself."""
    return "\n      | ".join(plain(text).splitlines() or [""])


def _describe(shown: dict[str, Any]) -> str:
    """The text a person decides on. Everything the hub or a server wrote goes through ``plain`` (no escape sequence or
    control character can redraw the prompt)."""
    item, kind = shown["item"], shown["kind"]
    if kind == "skill":
        body = (
            f"Company skill {plain(item['name'])!r}. This is the full text an agent would read:\n\n"
            f"{plain(item['text'])}\n"
        )
    else:
        sign_in = "no sign-in" if item["auth"] == "none" else f"company secret {plain(item['secret'])!r}"
        body = (
            f"Company tool server {plain(item['name'])!r}\n  address: {plain(item['url'])}\n  sign-in: {sign_in}\n"
            f"  for: {_block(item['description']) or '(not said)'}\n  every tool starts Off.\n"
        )
    changed = "".join(f"  changed since you turned it on: {plain(c['word'])}\n" for c in shown.get("changed", []))
    return body + changed + "\n" + "".join(f"- {n}\n" for n in shown["notes"]) + f"  digest {item['digest'][:23]}...\n"


def _schema_text(row: dict[str, Any]) -> str:
    return plain(json.dumps(row["inputSchema"], indent=2, ensure_ascii=False, sort_keys=True))


def _tool_text(row: dict[str, Any], server: str) -> str:
    """What the owner reviews for one tool: the whole pinned view (name, title, description, input schema)."""
    name = plain(row["name"])
    if row.get("problem") == "too_long":
        return f"Tool {name!r} of company server {plain(server)!r} is too long to review; it stays off.\n"
    lines = [f"Tool {name!r} of company server {plain(server)!r}"]
    if row.get("title"):
        lines.append(f"  title: {_block(row['title'])}")
    says_reads = row.get("reads") is True
    lines.append("  kind: " + ("reads (the server says it only reads)" if says_reads else "changes things"))
    lines.append(f"  description: {_block(row['description']) or '(none)'}")
    lines.append("  input schema (this is pinned too):")
    lines.extend("    " + ln for ln in _schema_text(row).splitlines())
    lines.append("  Its results are untrusted text from your company's server.")
    return "\n".join(lines) + "\n"


def _tools_text(shown: dict[str, Any]) -> str:
    """``show`` for a server: each tool seen, its status and (for a good one) the same review text as ``tool on``."""
    if shown["kind"] != "mcp" or not shown.get("tools"):
        return ""
    server = shown["item"]["name"]
    return "\ntools seen:\n" + "".join(f"\n[{plain(t['status'])}] " + _tool_text(t, server) for t in shown["tools"])


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
        if len(a.rest) != _ARGS[a.cmd] or (a.cmd != "tool" and a.rest and a.rest[0] not in ("skill", "mcp")):
            raise PubError("invalid.input", _USAGE, 2)
        if a.cmd == "tool" and a.rest[2] not in ("on", "off"):
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
            emit(data, _describe(data) + _tools_text(data) + f"status: {plain(data['status'])}")
        elif a.cmd == "tool":
            server, tool, want = a.rest
            args: dict[str, Any] = {"server": server, "tool": tool, "on": want == "on"}
            if want == "on":  # always on the owner's terminal, for exactly the pin that was shown
                shown = _call(target, "org.show", {"kind": "mcp", "name": server}, env)
                row = next((t for t in shown.get("tools", []) if t["name"] == tool), None)
                if row is None:
                    raise PubError("not_found", f"no tool {tool!r} seen on {server!r} yet: let an agent list its tools")
                text = _tool_text(row, server)
                if row.get("problem") == "too_long":
                    raise PubError("invalid.input", text.strip())
                if not review_prompt(text + "Let agents of this workspace use this tool?"):
                    raise PubError("sign.cancelled", "not turned on; nothing changed", 3)
                args["pin"] = row["pin"]
            data = _call(target, "org.tool", args, env)
            emit(data, f"ok {server} {tool} is {want.capitalize()}")
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
