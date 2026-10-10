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

from orch.ops.actors import EVENT_ACTORS
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
    "registry_emits",
    "verb_events",
]

_REGISTRY: dict[str, Operation] = {}
_LOADED = False


def _unimplemented(name: str) -> Handler:
    def handler(ctx: Context, args: dict) -> Result:
        raise NotImplementedYet(name)

    return handler


def _check_rules(op: Operation) -> None:
    """The actor rules of format doc 5.2 and 5.4, enforced for every operation that enters the registry."""
    from orch.ops.actors import allows

    if op.who == "read" and op.emits:
        raise ValueError(f"{op.name}: a read operation cannot emit events")
    for t in op.emits:
        if t not in EVENT_ACTORS or not allows(op.who, t):
            raise ValueError(f"{op.name}: who={op.who} may not append {t}")
    if op.who == "human" and "grant_valid" in op.pre:
        raise ValueError(f"{op.name}: a human operation is signed by the person, it has no grant")
    if op.who in ("agent", "unattended") and "user_presence" in op.pre:
        raise ValueError(f"{op.name}: an agent operation cannot need user presence")


def register(op: Operation) -> Operation:
    _check_rules(op)
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
    from orch.ops import commands

    try:
        for op in commands.collect():
            register(op)
    except Exception:
        _REGISTRY.clear()  # never leave a half-filled registry behind
        raise
    _LOADED = True


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


def verb_events() -> dict[str, frozenset[str]]:
    """The frozen table (``orch.model.emits``) that grant verbs are judged by; the live registry must equal it
    (a test checks), so replay never depends on a release's handlers."""
    from orch.model.emits import CURRENT

    return dict(CURRENT)


def registry_emits() -> dict[str, frozenset[str]]:
    """What the registry says each operation emits (operations that emit nothing left out), for the test above."""
    return {op.name: frozenset(op.emits) for op in all() if op.emits}


def iter_groups() -> Iterator[tuple[str, list[Operation]]]:
    seen: dict[str, list[Operation]] = {}
    for op in all():
        seen.setdefault(op.group, []).append(op)
    yield from seen.items()
