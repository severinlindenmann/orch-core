"""The public face of ``model/``: ``replay``, ``admit``, ``advance`` and ``at`` over an immutable ``State``.

Preconditions (the store's job, C3): every event passed in has been validated with ``orch.schema.validate`` and, for
replay, chain-checked. The model raises ``KeyError`` on a malformed event rather than guessing.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from . import engine, visibility
from .codes import OK, Code, Ok, Refusal
from .engine import Ctx, apply_event
from .types import WORKSPACE, Core, ts
from .verifier import Verifier
from .views import TicketView, WorkspaceView, ticket_view, workspace_view

# Workspace events that cannot change what an existing ticket view shows (so `advance` keeps those views).
_NO_TICKET_EFFECT = frozenset(
    {"device.added", "device.removed", "grant.issued", "projection.repaired", "invalid.acknowledged"}
)


@dataclass(frozen=True)
class ChainError:
    log: str
    seq: int
    code: str
    detail: str


@dataclass(frozen=True)
class State:
    """Derived state of a workspace at ``now`` (a timestamp string). Frozen; ``admit``, ``advance`` and ``at`` never
    change a State.

    ``tickets`` holds **every** ticket, restricted ones included: it is for the host. Anything that shows tickets to
    a person (CLI, dashboard, relay) must go through :meth:`visible_to` (§9).
    """

    workspace: WorkspaceView
    tickets: Mapping[str, TicketView]
    chain_errors: tuple[ChainError, ...]
    now: str
    _core: Core = field(repr=False, compare=False)
    _ctx: Ctx = field(repr=False, compare=False)

    def visible_to(self, person: str) -> Mapping[str, TicketView]:
        """The tickets ``person`` may see (§9): what a reader, the CLI or the relay may show them."""
        core = self._core
        return MappingProxyType(
            {u: t for u, t in self.tickets.items() if visibility.can_see(core.ws, core.tickets[u], person)}
        )

    def by_key(self, key: str) -> TicketView:
        """The ticket with this key (unfiltered, like ``tickets``). ``KeyError`` if there is none."""
        return self.tickets[self._core.keys[key]]


def _errors(core: Core) -> tuple[ChainError, ...]:
    return tuple(
        ChainError(log, lc.seq + 1, Code.CHAIN_BROKEN.value, lc.broken)
        for log, lc in sorted(core.logs.items())
        if lc.broken
    )


def _build(core: Core, ctx: Ctx, now: str) -> State:
    n = ts(now)
    tickets = MappingProxyType({uid: ticket_view(core, t, n) for uid, t in sorted(core.tickets.items())})
    return State(workspace_view(core, n), tickets, _errors(core), now, core, ctx)


def replay(
    workspace_events: Iterable[dict[str, Any]],
    ticket_logs: Mapping[str, Iterable[dict[str, Any]]],
    *,
    verifier: Verifier,
    now: str,
    expected_workspace_id: str,
    expected_genesis: str | None = None,
) -> State:
    """Derive the state from the parsed events of the workspace log and the ticket logs (schema-valid).

    The logs are walked in the merged order of §5.5: the workspace log first (by ``seq``), and ticket events by
    ``ws_seq``, then ``at``, then ticket uid, then ``seq`` (an event with ``ws_seq = k`` follows workspace event
    ``k``). ``admit`` only accepts an append that sorts after every earlier append, so for any log the host wrote,
    this order is the append order. ``expected_workspace_id`` and ``expected_genesis`` are the store's pins (from
    ``config.json`` and the host state dir); a genesis that does not match is refused before it is trusted.
    ``now`` is a timestamp string (the model reads no clock); it decides what is live in the views.
    Events that fail authorization are absent for state and reported (``workspace.invalid``,
    the tickets' ``frozen``); a broken chain stops that log (``chain_errors``).
    """
    ctx = Ctx(verifier, expected_workspace_id, expected_genesis)
    core = Core()
    merged: list[tuple[tuple[int, int, int, str, int], str, dict[str, Any]]] = [
        ((e["seq"], 0, 0, "", e["seq"]), WORKSPACE, e) for e in workspace_events
    ]
    for uid, events in ticket_logs.items():
        merged += [((e["ws_seq"], 1, ts(e["at"]), uid, e["seq"]), uid, e) for e in events]
    merged.sort(key=lambda x: x[0])
    for _, log, e in merged:
        apply_event(core, log, e, ctx, commit=True)
    return _build(core, ctx, now)


def admit(state: State, event: dict[str, Any], *, log: str) -> Ok | Refusal:
    """May the store append ``event`` to ``log`` (``WORKSPACE`` or a ticket uid) now? The same rules as replay.

    The event is judged at its own ``at`` (claims, leases and grants lapse by it), not at ``state.now``, which only
    shapes the views. It needs no ``host_sig`` yet. ``O(event)`` for a ticket event; a workspace event that changes
    every ticket (member, role, policy, addon, restore, compromised device) copies all tickets.
    """
    ctx = Ctx(state._ctx.verifier, state._ctx.expected_workspace_id, state._ctx.expected_genesis, admit=True)
    r = apply_event(copy.copy(state._core), log, event, ctx, commit=False)
    return OK if r is None else r


def external_edit_voids(state: State, uid: str, sections: dict[str, Any]) -> list[str] | Refusal:
    """The ``voided_gates`` an ``edit.external`` of ``uid`` with these ``sections`` would carry (§5.11), or the refusal
    (a bound section of a done or closed ticket, an unknown section). The store calls this to fill the event it
    appends; replay recomputes the same list and refuses a mismatch."""
    return engine.external_edit_voids(state._core, uid, sections)


def _fork(core: Core) -> Core:
    """A Core that shares everything with ``core`` except what a commit replaces or mutates (copy on write)."""
    return Core(
        ws=core.ws,
        tickets=core.tickets,
        keys=core.keys,
        created_at=core.created_at,
        last_pos=core.last_pos,
        logs=dict(core.logs),
    )


def advance(state: State, event: dict[str, Any], *, log: str, now: str | None = None) -> State:
    """The state after the store appended ``event`` (as replay sees it: a refused event becomes an invalid one).

    Incremental: only the views the event can change are rebuilt (the touched ticket, or every ticket for a
    workspace event that can change them); the rest are shared with ``state``.
    """
    core = _fork(state._core)
    apply_event(core, log, event, state._ctx, commit=True, cow=True)
    now = now or state.now
    n = ts(now)
    if log == WORKSPACE:
        ws = workspace_view(core, n)
        if event["type"] in _NO_TICKET_EFFECT and now == state.now:
            return State(ws, state.tickets, _errors(core), now, core, state._ctx)
        tickets = {uid: ticket_view(core, t, n) for uid, t in sorted(core.tickets.items())}
    else:
        ws = state.workspace if now == state.now else workspace_view(core, n)
        tickets = dict(state.tickets)
        if log in core.tickets:
            tickets[log] = ticket_view(core, core.tickets[log], n)
        if now != state.now:
            for uid, t in core.tickets.items():
                if t.claim is not None or t.leases:
                    tickets[uid] = ticket_view(core, t, n)
    return State(ws, MappingProxyType(dict(sorted(tickets.items()))), _errors(core), now, core, state._ctx)


def at(state: State, now: str) -> State:
    """The same derived state seen at another ``now``: only claims, leases and grants depend on it, so only the
    workspace view and the tickets holding a claim or a lease are rebuilt."""
    n = ts(now)
    core = state._core
    tickets = dict(state.tickets)
    for uid, t in core.tickets.items():
        if t.claim is not None or t.leases:
            tickets[uid] = ticket_view(core, t, n)
    return State(workspace_view(core, n), MappingProxyType(tickets), state.chain_errors, now, core, state._ctx)
