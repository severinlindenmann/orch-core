"""``orch update [--check] [--channel C] [--use VER] [--finish | --rollback] [--yes] [--json]``: a person's command.

Refused when ``ORCH_GRANT`` or ``ORCH_SESSION`` is set (the same guard as ``orch migrate``). The work is in
:mod:`orch.update.engine`; this module parses, asks, and prints plain words.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TextIO

from orch.ops.errors import ERRORS, OrchError
from orch.update.engine import Outcome, Seams, Updater

__all__ = ["main"]

USAGE = (
    "usage: orch update [--check] [--channel main|develop] [--yes] [--json]\n"
    "       orch update --use VERSION       use an installed version again (your data stays)\n"
    "       orch update --finish | --rollback   deal with an update that was interrupted\n"
)
AGENT_ENV = ("ORCH_GRANT", "ORCH_SESSION")
FLAGS = {"--check", "--yes", "--json", "--finish", "--rollback"}


def default_seams(env: Mapping[str, str], out: TextIO, err: TextIO) -> Seams:
    from orch.lifecycle.agents import live_runs
    from orch.lifecycle.units import Env
    from orch.update.services import GithubSource, LifecycleServices, run_orch, uv_install

    e = Env(env, out, err)
    return Seams(
        source=GithubSource(),
        services=LifecycleServices(e),
        installer=uv_install,
        run_orch=run_orch,
        live_runs=lambda state: live_runs(state, None),
    )


def _fail(exc: OrchError, as_json: bool, out: TextIO, err: TextIO) -> int:
    spec = ERRORS.get(exc.code)
    if as_json:
        out.write(json.dumps({"ok": False, "code": exc.code, "message": exc.message}) + "\n")
    else:
        err.write(f"orch update: {exc.message}\n")
    return spec.exit if spec else 1


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    seams: Seams | None = None,
    ask: Any = None,
) -> int:
    args = list(argv)
    as_json = "--json" in args
    if any(env.get(k) for k in AGENT_ENV):
        return _fail(
            OrchError("human_only", "a person updates orch at their own terminal; an agent cannot"), as_json, out, err
        )
    channel = use = None
    rest = []
    it = iter(args)
    for a in it:
        if a in ("--channel", "--use"):
            v = next(it, None)
            if v is None:
                err.write(USAGE)
                return 2
            channel, use = (v, use) if a == "--channel" else (channel, v)
        else:
            rest.append(a)
    if (
        any(a not in FLAGS for a in rest)
        or ("--finish" in rest and "--rollback" in rest)
        or (use is not None and (channel or {"--check", "--finish", "--rollback"} & set(rest)))
    ):
        err.write(USAGE)
        return 2
    u = Updater(env, seams or default_seams(env, out, err), out)
    try:
        if use is not None:
            return _report(u.use(use), as_json, out, err)
        if "--check" in rest:
            return _check(u, channel, as_json, out, env)
        assume = "finish" if "--finish" in rest else "rollback" if "--rollback" in rest else None
        if channel is not None and "--yes" not in rest and not _confirm(channel, ask, out):
            err.write("orch update: not changed\n")
            return 3
        return _report(u.update(channel, assume=assume), as_json, out, err)
    except OrchError as exc:
        return _fail(exc, as_json, out, err)


def _confirm(channel: str, ask: Any, out: TextIO) -> bool:
    if ask is None:
        return False  # no way to ask: the person says --yes
    return bool(ask(f"Switch to the '{channel}' channel and update? [y/N] "))


def _report(o: Outcome, as_json: bool, out: TextIO, err: TextIO) -> int:
    if as_json:
        out.write(
            json.dumps({"ok": o.ok, "action": o.action, "from": o.from_version, "to": o.to_version,
                        "message": o.message, "code": o.code, "warnings": o.warnings}) + "\n"
        )  # fmt: skip
    else:
        (out if o.ok else err).write(o.message + "\n")
    if o.ok:
        return 0
    return ERRORS[o.code].exit if o.code in ERRORS else 5


def _check(u: Updater, channel: str | None, as_json: bool, out: TextIO, env: Mapping[str, str]) -> int:
    rec, ch, ver = u.check(channel)
    cur = u.installed_version(rec)
    doc = ver.document
    newer = ver.version != cur
    import io

    from orch.migrate import cli as migrate_cli

    buf = io.StringIO()
    migrate_cli.main(["--check", "--json"], env=env, out=buf, err=io.StringIO())
    try:
        pending = json.loads(buf.getvalue()).get("roots", [])
    except ValueError:
        pending = []
    if as_json:
        row = {"ok": True, "installed": cur, "latest": ver.version, "channel": ch, "update_available": newer}
        row.update({"changelog": doc["changelog"], "roots": pending})
        out.write(json.dumps(row) + "\n")
        return 0
    out.write(f"installed {cur}, latest {ver.version} (channel {ch})\n")
    out.write("An update is available.\n" if newer else "You are up to date.\n")
    out.write(f"Changes: {doc['changelog']}\n")
    for row in pending:
        n = len(row.get("steps") or [])
        out.write(f"data: {row['root']}: {'up to date' if not n else f'{n} migration step(s) waiting'}\n")
    return 0
