"""The public face of ``model/``: ``replay``, ``admit`` and ``advance`` over an immutable ``State``."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from . import visibility
from .codes import OK, Ok, Refusal
from .engine import Ctx, apply_event
from .types import Core, ts
from .verifier import Verifier
from .views import TicketView, WorkspaceView, ticket_view, workspace_view


@dataclass(frozen=True)
class ChainError:
    log: str
    seq: int
    code: str
    detail: str


@dataclass(frozen=True)
class State:
    """Derived state of a workspace at ``now``. Frozen; ``admit`` and ``advance`` never change it."""

    workspace: WorkspaceView
    tickets: Mapping[str, TicketView]
    chain_errors: tuple[ChainError, ...]
    now: int
    _core: Core = field(repr=False, compare=False)
    _ctx: Ctx = field(repr=False, compare=False)

    def visible_to(self, person: str) -> Mapping[str, TicketView]:
        """The tickets ``person`` may see (§9): what a reader, the CLI or the relay may show them."""
        core = self._core
        return MappingProxyType(
            {u: t for u, t in self.tickets.items() if visibility.can_see(core.ws, core.tickets[u], person)}
        )

    def by_key(self, key: str) -> TicketView:
        return next(t for t in self.tickets.values() if t.key == key)


def _build(core: Core, ctx: Ctx, now: int) -> State:
    errs = tuple(
        ChainError(log, lc.seq + 1, "chain.broken", lc.broken) for log, lc in sorted(core.logs.items()) if lc.broken
    )
    return State(
        workspace_view(core, now),
        MappingProxyType({uid: ticket_view(core, t, now) for uid, t in sorted(core.tickets.items())}),
        errs,
        now,
        core,
        ctx,
    )


def replay(
    workspace_events: Iterable[dict[str, Any]],
    ticket_logs: Mapping[str, Iterable[dict[str, Any]]],
    *,
    verifier: Verifier,
    now: str,
    expected_genesis: str | None = None,
) -> State:
    """Derive the state from the parsed events of the workspace log and the ticket logs (already schema-valid).

    The logs are walked in the merged order of §5.5: the workspace log first, then ticket events by ``ws_seq`` and
    ``seq``. ``now`` is a timestamp string (the model reads no clock); it only decides what is live in the views.
    Events that fail authorization are absent for state and reported (``State.workspace.invalid`` and the
    tickets' ``frozen``); a broken chain stops that log (``State.chain_errors``).
    """
    ctx = Ctx(verifier, expected_genesis)
    core = Core()
    ws_events = list(workspace_events)
    merged: list[tuple[tuple[int, int, str, int], str, dict[str, Any]]] = [
        ((e["seq"], 0, "", e["seq"]), "workspace", e) for e in ws_events
    ]
    for uid, events in ticket_logs.items():
        merged += [((e["ws_seq"], 1, uid, e["seq"]), uid, e) for e in events]
    merged.sort(key=lambda x: x[0])
    for _, log, e in merged:
        apply_event(core, log, e, ctx, commit=True)
    return _build(core, ctx, ts(now))


def admit(state: State, event: dict[str, Any], *, log: str) -> Ok | Refusal:
    """May the store append ``event`` to ``log`` (``"workspace"`` or a ticket uid) now? The same rules as replay."""
    ctx = Ctx(state._ctx.verifier, state._ctx.expected_genesis, admit=True)
    r = apply_event(copy.copy(state._core), log, event, ctx, commit=False)
    return OK if r is None else r


def advance(state: State, event: dict[str, Any], *, log: str, now: str | None = None) -> State:
    """The state after the store appended ``event`` (as replay would see it: refused events become invalid ones)."""
    core = _clone(state._core)
    apply_event(core, log, event, Ctx(state._ctx.verifier, state._ctx.expected_genesis), commit=True)
    return _build(core, state._ctx, ts(now) if now else state.now)


def at(state: State, now: str) -> State:
    """The same derived state seen at another ``now``."""
    return _build(state._core, state._ctx, ts(now))


def _clone(core: Core) -> Core:
    return copy.deepcopy(core)
