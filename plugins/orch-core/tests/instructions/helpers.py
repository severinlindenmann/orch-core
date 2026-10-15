"""Shared checks: every ``orch <command> [--flags]`` an instruction text mentions exists in the operation registry."""

from __future__ import annotations

import re

import orch.ops as ops
from orch.cli.args import arg_specs

_START = re.compile(r"\borch (?=[a-z<])")
_END = re.compile(r"\||\n|`|·|\bthen:|\bfinished:|\borch\b")


def resolve_words(text: str):
    """``(op, rest)`` for the command words at the start of ``text`` (after ``orch ``), or ``None``."""
    words = [w.rstrip(",;:.)") for w in text.split()]
    for n in (2, 1):
        if len(words) >= n:
            op = ops.resolve(words[:n])
            if op is not None:
                return op, words[n:]
    return None


def command_references(text: str) -> list[tuple[str, object, list[str]]]:
    """Every ``orch ...`` mention as ``(mention, op or None, flags used)``. A mention ends at the next ``orch``, a
    pipe, a backtick or the end of the line. ``orch <cmd>`` and the version stamp ``orch v2.0`` are judged by their
    first words (the caller skips them)."""
    out = []
    for m in _START.finditer(text):
        tail = text[m.end() :]
        if re.match(r"v\d", tail):
            continue
        end = _END.search(tail)
        body = tail[: end.start()] if end else tail
        found = resolve_words(body)
        flags = re.findall(r"(?<![\w-])(--?[a-z][a-z-]*)", found and " ".join(found[1]) or "")
        out.append(("orch " + body.strip(), found[0] if found else None, flags))
    return out


def op_flags(op) -> set[str]:
    flags = {"--json", "--dry-run", "--help"}
    for a in arg_specs(op):
        if not a.positional:
            flags.add(a.flag)
            if a.short:
                flags.add(f"-{a.short}")
    return flags


def unknown_references(text: str) -> list[str]:
    bad = []
    for mention, op, flags in command_references(text):
        if mention.startswith("orch <"):
            continue
        if op is None:
            bad.append(f"{mention}: no such command")
            continue
        for f in flags:
            if f not in op_flags(op):
                bad.append(f"{mention}: {f} is not a flag of {op.cli}")
    return bad


def backticked(text: str) -> str:
    """The code spans of a prose text that start with ``orch``, one per line: in prose "orch" is a word, in a code span
    it is the command."""
    return "\n".join(c for c in re.findall(r"`([^`\n]+)`", text) if c.startswith("orch "))
