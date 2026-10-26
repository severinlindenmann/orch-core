"""``orch market``: the add-on marketplace (V3 G2, #920; docs/architecture/orch-v2-marketplace.md).

    orch market add URL [--commit SHA]       a person: add a marketplace; the index is fetched, verified and recorded
    orch market list [--entries]             a person or agent: the added marketplaces (and their add-ons), no network
    orch market refresh [URL] [--commit SHA] a person or agent: fetch an added marketplace again; --commit is a person's

``add`` and ``refresh --commit`` are refused to an agent (``ORCH_GRANT`` or ``ORCH_SESSION`` set) before the host is
asked, and the host refuses them again (column P only, workspace owner). Only the official marketplace can be added
for now. A marketplace's name, title and summaries are shown as "from the marketplace": the signature does not cover
them. This command installs nothing."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from orch.cli.publish import PubError, _call, _target
from orch.market import ADDONS

__all__ = ["main"]

_USAGE = "usage: orch market {add URL [--commit SHA] | list [--entries] | refresh [URL] [--commit SHA]} [--json]"
_ARGS = {"add": (1, 1), "list": (0, 0), "refresh": (0, 1)}


def _parser() -> argparse.ArgumentParser:
    class Quiet(argparse.ArgumentParser):
        def error(self, message: str) -> Any:  # type: ignore[override]
            raise PubError("invalid.input", f"{message}\n{_USAGE}", 2)

    p = Quiet(prog="orch market", add_help=False)
    p.add_argument("cmd", choices=tuple(_ARGS))
    p.add_argument("rest", nargs="*")
    p.add_argument("--commit")
    p.add_argument("--entries", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--workspace")
    return p


def _line(m: dict[str, Any]) -> str:
    if m.get("skipped"):
        return f"{m['url']}  skipped: fetched under five minutes ago"
    if m.get("error"):
        return f"{m['url']}  refused: {m['error']}"
    if m.get("cache") == "invalid":
        return f"{m['url']}  cache invalid: run orch market refresh"
    parts = [m["url"], m["class"]]
    if "version" in m:
        parts.append(f"v{m['version']}")
    if m.get("index_commit"):
        parts.append(f"commit {m['index_commit'][:12]}" + (" (pinned)" if m.get("pinned") else ""))
    if m.get("stale"):
        parts.append("STALE")
    if m.get("moved") is False and "head" in m:
        parts.append(f"head {m['head'][:12]}" + (" is newer, not moved (pinned)" if m.get("newer") else " unchanged"))
    if "entries" in m:
        parts.append(f"{m['entries']} add-ons")
    return "  ".join(parts)


def _text(op: str, data: dict[str, Any]) -> str:
    rows = [data] if op == "add" else data.get("markets", [])
    out = [_line(r) for r in rows] or ["no marketplaces added"]
    for r in rows:
        fm = r.get("from_marketplace")
        if fm:  # E9: display text of the index, not covered by its signature
            out.append(
                f"  from the marketplace (not signed): {json.dumps(fm.get('name'))} {json.dumps(fm.get('title'))}"
            )
        for a in r.get(ADDONS, []):
            tags = (" REVOKED" if a["revoked"] else "") + ("" if a["publisher"] else " unsigned")
            if a.get("installed"):  # G3: what is installed from this marketplace, and whether a newer entry exists
                tags += f" [installed {a['installed']['version']}, {a['installed']['state']}]"
                tags += " UPDATE" if a.get("update") else ""
            out.append(f"  {a['id']} {a['version']} [{','.join(a['capabilities'])}]{tags}")
    return "ok market " + op + "\n" + "\n".join(out)


def main(
    argv: list[str],
    *,
    env: Mapping[str, str],
    out: TextIO | None = None,
    err: TextIO | None = None,
    now: Any = None,
) -> int:
    out = out if out is not None else sys.stdout
    as_json = "--json" in argv

    def fail(exc: PubError) -> int:
        if as_json:
            doc = {"ok": False, "code": exc.code, "message": exc.message, **({"hint": exc.hint} if exc.hint else {})}
            out.write(json.dumps(doc) + "\n")
        else:
            out.write(f"error {exc.code}: {exc.message}\n" + (f"hint: {exc.hint}\n" if exc.hint else ""))
        return exc.exit

    from orch.ops.errors import ERRORS, OrchError

    try:
        a = _parser().parse_intermixed_args(argv)
        lo, hi = _ARGS[a.cmd]
        if not lo <= len(a.rest) <= hi or (a.commit and a.cmd == "list") or (a.entries and a.cmd != "list"):
            raise PubError("invalid.input", _USAGE, 2)
        agent = bool(env.get("ORCH_GRANT") or env.get("ORCH_SESSION"))
        if agent and (
            a.cmd == "add" or a.commit
        ):  # before the host is asked, so nothing is fetched on an agent's behalf
            raise PubError("market.actor_refused", ERRORS["market.actor_refused"].message, 3)
        target = _target(env)
        args: dict[str, Any] = {}
        if a.cmd == "list":
            args = {"entries": True} if a.entries else {}
        else:
            if a.rest:
                args["url"] = a.rest[0]
            if a.commit:
                args["commit"] = a.commit
        data = _call(target, "market." + a.cmd, args, env)
        out.write(json.dumps({"ok": True, "data": data}) + "\n" if as_json else _text(a.cmd, data) + "\n")
        return 0
    except PubError as exc:
        return fail(exc)
    except OrchError as exc:
        return fail(PubError(exc.code, str(exc.message), getattr(exc, "wire_exit", 3), getattr(exc, "hint", None)))
