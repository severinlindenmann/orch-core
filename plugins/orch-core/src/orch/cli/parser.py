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

__all__ = ["Parsed", "build_parser", "parse", "resolve_command", "split_globals"]


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


class _Extend(argparse.Action):
    """``--options a,b`` and ``--options a --options b`` both give ``["a", "b"]``."""

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
        items = list(getattr(namespace, self.dest, None) or [])
        for v in [values] if isinstance(values, str) else values:
            items.extend(x for x in v.split(",") if x)
        setattr(namespace, self.dest, items)


def _add(p: argparse.ArgumentParser, a: Arg) -> None:
    kw: dict[str, Any] = {"dest": a.name, "default": None}
    if a.positional:
        if a.kind == "list":
            kw.update(nargs="+" if a.required else "*", action=_Extend)
        else:
            kw.update(nargs=None if a.required else "?")
        if a.kind == "int":
            kw["type"] = int
        if a.choices:
            kw["choices"] = a.choices
        p.add_argument(a.name, **{k: v for k, v in kw.items() if k != "dest"})
        return
    names = [a.flag] + ([f"-{a.short}"] if a.short else [])
    if a.kind == "bool":
        p.add_argument(*names, dest=a.name, action="store_true", default=None)
        return
    if a.kind == "list":
        kw["action"] = _Extend
    if a.kind == "int":
        kw["type"] = int
    if a.choices:
        kw["choices"] = a.choices
    kw["required"] = a.required
    p.add_argument(*names, **kw)


def build_parser(op: Operation) -> argparse.ArgumentParser:
    p = _Parser(prog=f"orch {op.cli}", cmd=op.name, add_help=False, allow_abbrev=False)
    for a in arg_specs(op):
        _add(p, a)
    if op.is_write:
        p.add_argument("--dry-run", dest="dry_run", action="store_true", default=None)
    return p


def split_globals(argv: list[str]) -> tuple[list[str], bool]:
    """Take ``--json`` out of the command line wherever it stands."""
    rest = [t for t in argv if t != "--json"]
    return rest, len(rest) != len(argv)


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
        raise UsageError(f"orch {head}: expected one of {', '.join(siblings)}", None)
    raise OrchError("unknown_command", f"unknown command {words[0]!r}")


def parse(argv: list[str]) -> Parsed:
    """Parse a command line into the operation and its arguments (validation against the schema comes later)."""
    tokens, as_json = split_globals(list(argv))
    op, rest = resolve_command(tokens)
    parser = build_parser(op)
    ns = vars(parser.parse_args(rest))
    dry = bool(ns.pop("dry_run", None))
    args: dict[str, Any] = {k: v for k, v in ns.items() if v is not None and v != []}
    for a in arg_specs(op):
        if a.name not in args and a.default is not None:
            args[a.name] = a.default
    return Parsed(op=op, args=args, json=as_json, dry_run=dry, argv=tokens)
