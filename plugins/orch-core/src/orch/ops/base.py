"""The operation type, the handler contract, and what a handler receives and returns."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Context", "Handler", "NotImplementedYet", "Operation", "Result", "WHO", "cli_words"]

WHO = ("agent", "unattended", "human", "read")


class NotImplementedYet(NotImplementedError):
    """The operation is declared but its handler is not written yet (C6 agent operations, C7 human ones)."""

    def __init__(self, op: str) -> None:
        super().__init__(f"{op}: not implemented yet")
        self.op = op


@dataclass
class Context:
    """What the CLI hands a handler. Secrets never reach output: ``grant`` is redacted by the renderer."""

    session: str | None = None
    grant: str | None = field(default=None, repr=False)
    human_presence: bool = False
    dry_run: bool = False
    now: Callable[[], float] = time.time
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: The retry-dedup key of this call (set by the CLI for writes with a session). A handler passes it to
    #: ``Store.append(idem=...)`` so a retry after a crash can never append twice.
    idem: str | None = field(default=None, repr=False)
    #: The workspace of this call (``orch.ops.runtime.Workspace``), set by the CLI: opened lazily, shared by the hooks
    #: and the handler, so one call opens the store once. ``None`` in a test that builds a Context by hand.
    workspace: Any = field(default=None, repr=False)


@dataclass
class Result:
    """A successful result. ``data`` is the ``--json`` payload; ``lines`` are extra text-mode lines (between the
    ``ok`` line and the ``next:`` line) that the JSON output does not carry; ``hints[0]`` is the ``next:`` line;
    ``exit`` is the process exit code (0, except for example a ``wait`` that ends in ``changes_requested``: 3)."""

    data: Any = field(default_factory=dict)
    key: str | None = None
    seq: int = 0
    cursor: int = 0
    hints: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    duplicate: bool = False
    exit: int = 0


Handler = Callable[[Context, dict[str, Any]], Result]


def cli_words(name: str) -> tuple[str, ...]:
    """``task.done`` is ``orch task done``; ``request_changes`` is ``orch request-changes``."""
    return tuple(part.replace("_", "-") for part in name.split("."))


@dataclass(frozen=True)
class Operation:
    """One registry entry: the declaration (validated against the ``operation`` schema), its place in the command
    table, and the handler. The declaration is a plain dict so ``describe`` and later an MCP addon read it as is."""

    declaration: Mapping[str, Any]
    group: str
    handler: Handler

    def validate(self) -> None:
        """Check the declaration against the ``operation`` schema (slow, about 0.1 s: tests call it, startup
        does not). Raises :class:`orch.schema.SchemaError`."""
        from orch import schema

        schema.validate("operation", dict(self.declaration))

    @property
    def name(self) -> str:
        return self.declaration["name"]

    @property
    def who(self) -> str:
        return self.declaration["who"]

    @property
    def input(self) -> dict[str, Any]:
        return self.declaration["input"]

    @property
    def summary(self) -> str:
        return self.declaration["input"].get("description", "")

    @property
    def pre(self) -> list[str]:
        return self.declaration["pre"]

    @property
    def emits(self) -> list[str]:
        return self.declaration["emits"]

    @property
    def output(self) -> dict[str, Any]:
        return self.declaration["output"]

    @property
    def errors(self) -> list[dict[str, Any]]:
        return self.declaration["errors"]

    @property
    def words(self) -> tuple[str, ...]:
        return cli_words(self.name)

    @property
    def cli(self) -> str:
        return " ".join(self.words)

    @property
    def is_write(self) -> bool:
        return self.who != "read"
