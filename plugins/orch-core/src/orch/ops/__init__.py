"""The operation registry: one module per operation with its schema, permission, preconditions and events.

* ``register(op)`` adds an :class:`Operation` (a duplicate name is refused); ``get(name)`` finds it by registry name
  (``task.done``); ``all()`` lists them in command-table order. The declaration modules in ``orch.ops.commands``
  register themselves the first time the registry is used.
* Every handler starts as one that raises :class:`NotImplementedYet`; ``bind(name, handler)`` replaces it
  (C6 and C7 bind the real ones).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

from orch.ops.base import Context, Handler, NotImplementedYet, Operation, Result

__all__ = [
    "Context",
    "NotImplementedYet",
    "Operation",
    "Result",
    "all",
    "bind",
    "get",
    "names",
    "register",
    "resolve",
]

_REGISTRY: dict[str, Operation] = {}
_LOADED = False


def _unimplemented(name: str) -> Handler:
    def handler(ctx: Context, args: dict) -> Result:
        raise NotImplementedYet(name)

    return handler


def register(op: Operation) -> Operation:
    if op.name in _REGISTRY:
        raise ValueError(f"operation {op.name!r} is already registered")
    for other in _REGISTRY.values():
        if other.words == op.words:
            raise ValueError(f"operation {op.name!r} has the same command words as {other.name!r}")
    _REGISTRY[op.name] = op
    return op


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    from orch.ops import commands

    for op in commands.collect():
        register(op)


def get(name: str) -> Operation:
    _load()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown operation {name!r}") from None


def all() -> list[Operation]:  # noqa: A001
    _load()
    return list(_REGISTRY.values())


def names() -> list[str]:
    return [op.name for op in all()]


def bind(name: str, handler: Handler) -> Operation:
    """Replace an operation's handler (the declaration stays). Returns the updated entry."""
    op = dataclasses.replace(get(name), handler=handler)
    _REGISTRY[name] = op
    return op


def resolve(words: list[str] | tuple[str, ...]) -> Operation | None:
    """The operation for command words (``["task", "done"]``), or ``None``. A dotted or underscored form is also
    accepted (``task.done``, ``request_changes``)."""
    _load()
    flat: list[str] = []
    for w in words:
        flat.extend(w.split("."))
    target = tuple(w.replace("_", "-") for w in flat)
    for op in _REGISTRY.values():
        if op.words == target:
            return op
    return None


def iter_groups() -> Iterator[tuple[str, list[Operation]]]:
    seen: dict[str, list[Operation]] = {}
    for op in all():
        seen.setdefault(op.group, []).append(op)
    yield from seen.items()
