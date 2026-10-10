"""Arguments derived from an operation's input schema: one :class:`Arg` per property.

The rules (the only place they live):

* a property named in ``x-positional`` is positional, in that order; every other property is a ``--flag``
  (underscores become hyphens), optionally with a one-letter ``x-short``;
* ``boolean`` is a switch, ``integer`` takes a number, ``array`` of strings takes values (a repeated flag adds to
  it; ``x-split`` also lets one value hold a comma separated list), an ``enum`` limits the choices, ``string`` is
  text; any other type is refused when the parser is built; a scalar flag given twice is a usage error;
* a name in ``required`` must be given (a missing one is a usage error);
* ``default`` fills a missing argument after parsing;
* ``x-metavar`` is the placeholder shown in usage, default the upper-cased name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orch.ops import Operation

__all__ = ["Arg", "arg_specs", "usage"]


@dataclass(frozen=True)
class Arg:
    name: str
    flag: str | None
    short: str | None
    positional: bool
    required: bool
    kind: str  # "str", "int", "bool", "list"
    choices: tuple[str, ...] | None
    metavar: str
    help: str
    default: Any
    split: str | None = None
    needs_grant: bool = False

    @property
    def label(self) -> str:
        return self.metavar if self.positional else (self.flag or "")


def arg_specs(op: Operation) -> list[Arg]:
    schema = op.input
    props: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    positional = list(schema.get("x-positional", []))
    ordered = positional + [n for n in props if n not in positional]
    out: list[Arg] = []
    for name in ordered:
        if name not in props:
            raise ValueError(f"{op.name}: x-positional names {name!r}, which is not a property")
        p = props[name]
        t = p.get("type")
        kind = (
            {"string": "str", "boolean": "bool", "integer": "int", "array": "list"}.get(t)
            if isinstance(t, str)
            else None
        )
        if kind is None:
            raise ValueError(f"{op.name}: property {name!r} has type {t!r}, which the CLI cannot take")
        choices = tuple(p["enum"]) if "enum" in p else None
        is_pos = name in positional
        out.append(
            Arg(
                name=name,
                flag=None if is_pos else "--" + name.replace("_", "-"),
                short=p.get("x-short"),
                positional=is_pos,
                required=name in required,
                kind=kind,
                choices=choices,
                metavar=p.get("x-metavar") or name.upper(),
                help=p.get("description", ""),
                default=p.get("default"),
                split=p.get("x-split"),
                needs_grant=bool(p.get("x-needs-grant")),
            )
        )
    return out


def _piece(a: Arg) -> str:
    meta = "|".join(a.choices) if a.choices and len(a.choices) <= 4 else a.metavar
    if a.positional:
        body = f"{meta}..." if a.kind == "list" else meta
        return body if a.required else f"[{body}]"
    body = (a.flag or "") if a.kind == "bool" else f"{a.flag} {meta}"
    return body if a.required else f"[{body}]"


def usage(op: Operation) -> str:
    """``orch task done TASK [--run] [--artifact PATH] ...`` (``--dry-run`` on every write)."""
    parts = ["orch", *op.words]
    specs = arg_specs(op)
    parts += [_piece(a) for a in specs if a.positional]
    parts += [_piece(a) for a in specs if not a.positional]
    if op.is_write:
        parts.append("[--dry-run]")
    return " ".join(parts)
