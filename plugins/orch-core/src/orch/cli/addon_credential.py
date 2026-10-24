"""``orch addon login NAME`` and ``orch addon logout NAME``: the key an add-on may hold (V3 G6, design-g6 section 5).

    orch addon login NAME [--workspace W] [--state DIR]    store the key you type, for that add-on only
    orch addon logout NAME [--workspace W] [--state DIR]   delete it

A person's command, on the person's own terminal. There is no host op, no dashboard form and no hub path that writes
the key, so an agent cannot plant one. With ``ORCH_GRANT`` or ``ORCH_SESSION`` set the command is refused FIRST (before
the host or a terminal is touched). The value is read from ``/dev/tty`` with echo off: never from stdin, an argument or
the environment. ``login`` needs the add-on granted, enabled and holding the ``credential`` capability (asked of the
host as ``addon.list``, which only reads the signed log); only the shape of the value is checked. Everything is written
through :mod:`orch.addons.credential`; the audit line carries the name and the time, never the value."""

from __future__ import annotations

import argparse
import os
import resource
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from orch.addons import credential
from orch.custody import NoPrompt
from orch.host import securitylog
from orch.host.errors import HostError
from orch.market import ADDONS
from orch.ops.workspace_init import Terminal
from orch.runner.cli import _state, _workspace

__all__ = ["main"]

TERMINAL = Terminal()
USAGE = "usage: orch addon {login|logout} NAME [--workspace W] [--state DIR]"
PROMPT = "Key for {name} (not shown): "


class _Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # type: ignore[override]
        raise _Refusal("invalid.input", USAGE)


def _parse(args: list[str]) -> argparse.Namespace:
    p = _Parser(prog="orch addon", add_help=False, allow_abbrev=False)
    p.add_argument("cmd", choices=("login", "logout"))
    p.add_argument("name")
    p.add_argument("--workspace")
    p.add_argument("--state", type=Path)
    return p.parse_args(args)


def _no_core_dump() -> None:
    """A crash while the key is in memory must not write it to a core file."""
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass


def _row(env: Mapping[str, str], workspace: str | None, name: str) -> dict[str, Any] | None:
    """The ``addon.list`` row of ``name`` (the host replays and verifies the log), or ``None``."""
    from orch.cli.publish import PubError, _call, _target

    local = dict(env)
    if workspace:
        local["ORCH_WORKSPACE"] = workspace
    try:
        data = _call(_target(local), "addon.list", {}, local)
    except PubError as exc:
        raise _Refusal(exc.code, exc.message) from None
    return next((r for r in data.get(ADDONS, []) if r.get("name") == name), None)


def _login(a: argparse.Namespace, env: Mapping[str, str], state: Path, wid: str, out: TextIO, now: Any) -> None:
    row = _row(env, a.workspace, a.name)
    if row is None or row.get("state") != "active" or row.get("credential") is None:
        raise _Refusal(
            "invalid.input",
            f"{a.name} is not an enabled add-on that asks for a key (it must hold the credential grant)",
        )
    try:
        raw = TERMINAL._read(PROMPT.format(name=a.name), echo=False)  # noqa: SLF001 - /dev/tty only
    except NoPrompt:
        raise _Refusal("invalid.input", "orch addon login needs your own terminal (it never reads stdin)") from None
    value = raw.rstrip("\r\n")
    if not credential.valid(value):
        raise _Refusal("invalid.input", f"a key is 1 to {credential.MAX_LEN} printable characters without spaces")
    uid = os.getuid()
    credential.store(state, wid, uid, a.name, credential.Secret(value), now())
    securitylog.record(state, uid, now(), "addon.login", addon=a.name)
    out.write(
        f"Key stored. Only {a.name} gets this key, when orch runs it. Remove it with: orch addon logout {a.name}\n"
    )


def main(
    args: list[str],
    *,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    now: Any = time.time,
) -> int:
    try:
        if env.get("ORCH_GRANT") or env.get("ORCH_SESSION"):  # first: nothing is read, asked or opened for an agent
            raise _Refusal("human_only", "add-on keys are person-only; this stops accidents, it is not a sandbox")
        a = _parse(args)
        if credential.NAME.fullmatch(a.name) is None:
            raise _Refusal("invalid.input", "not an add-on name")
        _no_core_dump()
        uid = os.getuid()
        state = _state(a.state, env)
        wid, _ = _workspace(state, uid, a.workspace, env)
        if a.cmd == "login":
            _login(a, env, state, wid, out, now)
        else:
            removed = credential.remove(state, wid, a.name)
            securitylog.record(state, uid, now(), "addon.logout", addon=a.name)
            out.write("Key removed.\n" if removed else "No key was stored.\n")
        return 0
    except _Refusal as exc:
        err.write(f"orch addon: {exc.code}: {exc.message}\n")
    except HostError as exc:
        code = exc.code if exc.code in {"not_found", "custody.unsafe_mode", "invalid.input"} else "invalid.input"
        err.write(f"orch addon: {code}: key custody or workspace refused\n")
    except Exception as exc:
        err.write(f"orch addon: unexpected error ({type(exc).__name__})\n")
    return 1
