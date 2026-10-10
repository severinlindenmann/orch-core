"""The command line parser, generated from the registry: nothing here knows a single command by name."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

import orch.ops as ops
from orch.cli.args import Arg, arg_specs
from orch.cli.errors import UsageError
from orch.ops import Operation
from orch.ops.errors import OrchError

__all__ = ["Parsed", "Scan", "build_parser", "parse", "resolve_command", "scan"]

GLOBAL_FLAGS = ("--json", "--help", "-h")


@dataclass
class Parsed:
    op: Operation
    args: dict[str, Any]
    json: bool = False
    dry_run: bool = False
    argv: list[str] = field(default_factory=list)


class _Parser(argparse.ArgumentParser):
    """argparse that raises :class:`UsageError` instead of printing and exiting."""

    def __init__(self, *a: Any, cmd: str | None = None, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._cmd = cmd

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(f"{self.prog}: {message}", self._cmd)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        raise UsageError(message or f"{self.prog}: exit", self._cmd)


class _Once(argparse.Action):
    """A scalar flag given twice is refused, so a repeated ``--reason`` cannot silently change what is signed."""

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"argument {option_string}: given more than once")
        setattr(namespace, self.dest, values)


def _extender(split: str | None) -> type[argparse.Action]:
    class Extend(argparse.Action):
        """Adds every value; with ``x-split``, ``a,b`` is two values. Items are **not** stripped (a token with a
        space in it is refused by its pattern, F1 10.4 item 13); only empty items (``a,,b``, ``a,``) are dropped."""

        def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
            items = list(getattr(namespace, self.dest, None) or [])
            for v in [values] if isinstance(values, str) else values:
                if split:
                    items.extend(x for x in v.split(split) if x)
                else:
                    items.append(v)
            setattr(namespace, self.dest, items)

    return Extend


def _add(p: argparse.ArgumentParser, a: Arg) -> None:
    kw: dict[str, Any] = {"default": None}
    if a.positional:
        if a.kind == "list":
            kw.update(nargs="+" if a.required else "*", action=_extender(a.split))
        else:
            kw.update(nargs=None if a.required else "?")
        if a.kind == "int":
            kw["type"] = int
        if a.choices:
            kw["choices"] = a.choices
        p.add_argument(a.name, **kw)
        return
    names = [a.flag] + ([f"-{a.short}"] if a.short else [])
    if a.kind == "bool":
        p.add_argument(*names, dest=a.name, action="store_true", default=None)
        return
    kw["action"] = _extender(a.split) if a.kind == "list" else _Once
    if a.kind == "int":
        kw["type"] = int
    if a.choices:
        kw["choices"] = a.choices
    kw["required"] = a.required
    p.add_argument(*names, dest=a.name, **kw)


def build_parser(op: Operation) -> argparse.ArgumentParser:
    p = _Parser(prog=f"orch {op.cli}", cmd=op.name, add_help=False, allow_abbrev=False)
    for a in arg_specs(op):
        _add(p, a)
    if op.is_write:
        p.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    return p


def resolve_command(tokens: list[str]) -> tuple[Operation, list[str]]:
    """Longest command words first: ``task done T3`` is ``task.done``; ``show T3`` is ``show`` with an argument."""
    words: list[str] = []
    for t in tokens:
        if t.startswith("-"):
            break
        words.append(t)
        if len(words) == 2:
            break
    for n in range(len(words), 0, -1):
        op = ops.resolve(words[:n])
        if op:
            return op, tokens[n:]
    if not words:
        raise UsageError("no command", None)
    head = words[0].replace("_", "-").split(".")[0]
    siblings = sorted({o.words[1] for o in ops.all() if len(o.words) == 2 and o.words[0] == head})
    if siblings:
        raise UsageError(f"orch {head}: expected one of {', '.join(siblings)}", f"{head}")
    raise OrchError("unknown_command", f"unknown command {words[0]!r}")


@dataclass
class Scan:
    """A command line with the global flags taken out. ``op`` is ``None`` for a bare ``orch``/``orch --help``."""

    op: Operation | None
    rest: list[str]
    json: bool = False
    help: bool = False


def scan(argv: list[str]) -> Scan:
    """Find the command and take ``--json``, ``--help`` and ``-h`` out of its arguments.

    The globals count only before a ``--`` and only where they are flags: a token that follows a flag taking a value
    (``-m --help``) is left for the parser, which refuses it, and everything after ``--`` is data.
    """
    json_flag = help_flag = False
    lead = 0
    while lead < len(argv) and argv[lead] in GLOBAL_FLAGS:
        json_flag |= argv[lead] == "--json"
        help_flag |= argv[lead] != "--json"
        lead += 1
    tokens = argv[lead:]
    if not tokens or tokens[0].startswith("-"):
        return Scan(None, tokens, json_flag, help_flag)
    op, rest = resolve_command(tokens)
    valued = set()
    for a in arg_specs(op):
        if not a.positional and a.kind != "bool":
            valued.add(a.flag)
            if a.short:
                valued.add(f"-{a.short}")
    out: list[str] = []
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "--":
            out.extend(rest[i:])
            break
        if t in valued and i + 1 < len(rest):
            out.extend(rest[i : i + 2])
            i += 2
            continue
        if t in GLOBAL_FLAGS:
            json_flag |= t == "--json"
            help_flag |= t != "--json"
        else:
            out.append(t)
        i += 1
    return Scan(op, out, json_flag, help_flag)


def parse(argv: list[str]) -> Parsed:
    """Parse a command line into the operation and its arguments (validation against the schema comes later)."""
    sc = scan(list(argv))
    if sc.op is None:
        raise UsageError("no command", None)
    parser = build_parser(sc.op)
    ns = vars(parser.parse_args(sc.rest))
    dry = bool(ns.pop("dry_run", None))
    args: dict[str, Any] = {k: v for k, v in ns.items() if v is not None and v != []}
    for a in arg_specs(sc.op):
        if a.name not in args and a.default is not None:
            args[a.name] = a.default
    return Parsed(op=sc.op, args=args, json=sc.json, dry_run=dry, argv=list(argv))
