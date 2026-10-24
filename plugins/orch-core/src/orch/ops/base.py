"""The operation type, the handler contract, and what a handler receives and returns."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from orch.ops.caps import Capabilities

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
    #: Standard input read once (``--file -``), so the dedup key and the handler see the same bytes.
    files: dict[str, bytes] = field(default_factory=dict, repr=False)
    #: One line of progress for a long-running operation (``import v1`` while it signs), written to stderr by the CLI;
    #: never part of the result. ``None``: no progress is shown.
    progress: Callable[[str], None] | None = field(default=None, repr=False)
    #: What the handler may do (D-P2-9): environment, files, exec, git, signer, clock. ``None``: direct mode, built from
    #: ``env`` and ``now`` (today's CLI powers). The host passes :meth:`Capabilities.host`. Handlers use this, never
    #: ``os.environ``, ``open`` or ``subprocess``.
    caps: Capabilities = field(default=None, repr=False)  # type: ignore[assignment]
    #: Host mode (H4a1): the dispatcher derived the viewer itself (§0.5): the mapped person (column P), the verified
    #: grant's person (G) or ``None`` (no-person, N). Reads are filtered for it; direct mode leaves ``hosted`` False and
    #: takes the person from the grant, as P1 does.
    hosted: bool = False
    viewer: str | None = None
    #: Host mode: the principal (§0.5) that session notes are keyed by, together with the session label (F4).
    #: ``None`` in direct mode (keyed by session, as in P1).
    principal: str | None = field(default=None, repr=False)
    #: Host mode: called with the events a write is about to append, as ``(log, event id, type, actor)`` tuples plus
    #: the ticket key when known, **before** the first of them is appended, so the outcome record lists them (§0.7).
    plan: Callable[[list[tuple[str, str, str, dict[str, Any]]], str | None], None] | None = field(
        default=None, repr=False
    )
    #: Host mode: registry ops the host does not serve (``import.v1``). No other op may reach them, so
    #: ``apply`` refuses a batch item naming one.
    unserved: frozenset[str] = field(default=frozenset(), repr=False)
    #: Host mode (H6a): ``wait`` looks once per call and raises ``Parked`` instead of sleeping on the worker thread
    #: (:class:`orch.ops.commands.wait.Step`); ``None``: the P1 loop.
    wait_step: Any = field(default=None, repr=False)
    #: Host mode, ``human.prepare`` (H5a): a :class:`orch.ops.prepare.Capture`. The handler runs as the mapped person
    #: and **appends nothing**: each event it would sign or append is judged and recorded there instead, for the
    #: signing client to sign and ``human.submit`` to append. ``None`` everywhere else.
    prepare: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.caps is None:
            self.caps = Capabilities.direct(self.env, self.now)
        else:
            self.env = self.caps.env
            if self.now is time.time:  # the host's clock, unless the caller chose one
                self.now = self.caps.clock


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
    #: Text mode only: replaces the ``ok ...`` first line. A command that exits non-zero because it found problems
    #: (``doctor``, ``check``, a partial ``import v1``) says so in plain words instead of starting with ``ok``.
    head: str | None = None


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
