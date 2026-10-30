"""The public face of ``model/``: ``replay``, ``admit``, ``advance`` and ``at`` over an immutable ``State``.

Preconditions (the store's job, C3): every event passed in has been validated with ``orch.schema.validate`` and, for
replay, chain-checked. The model raises ``KeyError`` on a malformed event rather than guessing.
"""

from __future__ import annotations

import copy
import dataclasses
import heapq
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from . import engine, visibility
from .codes import OK, Ok, Refusal
from .engine import Ctx, apply_event
from .types import WORKSPACE, Core, ts
from .verifier import Verifier
from .views import TicketView, WorkspaceView, ticket_view, workspace_view

# Workspace events that cannot change what an existing ticket view shows (so `advance` keeps those views).
_NO_TICKET_EFFECT = frozenset(
    {
        "device.added",
        "device.removed",
        "device.exposed",
        "device.rotated",
        "grant.issued",
        "grant.requested",
        "addon.requested",
        "projection.repaired",
        "invalid.acknowledged",
        "hub.joined",
        "hub.left",
        "remote.changed",
        "remote.disabled",
        "remote.code_issued",
        "remote.code_burned",
        "remote.device_added",
        "remote.device_removed",
        "remote.key_rotated",
        "remote.session_opened",
        "remote.session_closed",
        "remote.presence",
    }
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

    def revocation_evidence(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Effective revocations, including permanent tombstones, keyed by ``(d_device, p_person)`` (§5.10); detached
        from replay state."""
        return {
            (device, person): copy.deepcopy(entry)
            for device, by_person in self._core.ws.revocations.items()
            for person, entry in by_person.items()
        }

    def revoked_reason(self, device: str, person: str) -> str | None:
        """The reason of the workspace's effective revocation of exactly ``(d_device, p_person)`` (a verified F1
        ``device.revoked`` or a restore's tombstone), or ``None``."""
        entry = self._core.ws.revocations.get(device, {}).get(person)
        return None if entry is None else str(entry["revocation"]["o"]["reason"])

    def knows_person(self, person: str) -> bool:
        """Whether a person key for ``p_person`` is known: a current or past member, or the person of a revocation
        entry (a restore tombstone carries the person's key)."""
        ws = self._core.ws
        return person in ws.members or person in ws.former or any(person in by for by in ws.revocations.values())

    def has_revocations_for(self, device: str) -> bool:
        """Whether ``ws.revocations`` holds any person's revocation of ``device`` (a restore's tombstone included)."""
        return bool(self._core.ws.revocations.get(device))

    def device_person(self, device: str) -> str | None:
        """The ``p_`` person a live (known, not tombstone-only) device is associated with, or None."""
        dev = self._core.ws.devices.get(device)
        return dev.person if dev is not None else None

    def is_member(self, person: str) -> bool:
        """Whether ``person`` (``p_`` id) is a current or past member of the workspace."""
        ws = self._core.ws
        return person in ws.members or person in ws.former

    def device_evidence(self, device: str) -> dict[str, Any]:
        ws = self._core.ws
        dev = ws.devices[device]
        person = ws.members.get(dev.person) or ws.former[dev.person]
        return {"device_cert": copy.deepcopy(dev.cert), "pk_pub": person.pk_pub}

    def person_key(self, person: str) -> str | None:
        """The person key (``pk_pub``, b64u) of a current member, else ``None`` (H12d-3: a pairing declaration signed
        for a ``pk_pin`` counts only under the key that pin names)."""
        member = self._core.ws.members.get(person)
        return None if member is None else member.pk_pub

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
        ChainError(log, lc.seq + 1, lc.broken_code, lc.broken) for log, lc in sorted(core.logs.items()) if lc.broken
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
    verb_events: Mapping[str, Iterable[str]] | None = None,
    invalidations: tuple[str, Callable[[str, str], None]] | None = None,
    hub_dev: bool = False,
) -> State:
    """Derive the state from the parsed events of the workspace log and the ticket logs (schema-valid).

    The logs are walked in the merged order of §5.5: the workspace log first (by ``seq``), and ticket events by
    ``ws_seq``, then ``at``, then ticket uid, then ``seq`` (an event with ``ws_seq = k`` follows workspace event
    ``k``). ``admit`` only accepts an append that sorts after every earlier append, so for any log the host wrote,
    this order is the append order. ``expected_workspace_id`` and ``expected_genesis`` are the store's pins (from
    ``config.json`` and the host state dir); a genesis that does not match is refused before it is trusted.
    ``now`` is a timestamp string (the model reads no clock); it decides what is live in the views.
    ``invalidations`` optionally observes only new pending voids for one ticket as ``(gate, event type)``.
    Events that fail authorization are absent for state and reported (``workspace.invalid``,
    the tickets' ``frozen``); a broken chain stops that log (``chain_errors``).
    """
    ctx = Ctx(verifier, expected_workspace_id, expected_genesis, verb_events=verb_events, hub_dev=hub_dev)
    core = Core()
    workspace_events = list(workspace_events)
    # Each log's lines are walked in file order, never sorted (§5.5 Reading); the logs are merged by the key below.
    # ``heapq.merge`` only picks the smallest head, so a line out of place in its log stays out of place and breaks it.
    streams = [
        [((e["seq"], 0, 0, "", e["seq"]), WORKSPACE, e) for e in workspace_events],
        *(
            [((e["ws_seq"], 1, ts(e["at"]), uid, e["seq"]), uid, e) for e in events]
            for uid, events in ticket_logs.items()
        ),
    ]
    pending: dict[str, set[str]] = {}
    for _, log, e in heapq.merge(*streams, key=lambda x: x[0]):
        apply_event(core, log, e, ctx, commit=True)
        if invalidations is not None:
            uid, note = invalidations
            ticket = core.tickets.get(uid)
            if ticket is not None:
                for gate, value in ticket.gates.items():
                    if value.pending_void - pending.get(gate, set()):
                        note(gate, e["type"])
                    pending[gate] = set(value.pending_void)
    return _build(core, ctx, now)


def admit(state: State, event: dict[str, Any], *, log: str) -> Ok | Refusal:
    """May the store append ``event`` to ``log`` (``WORKSPACE`` or a ticket uid) now? The same rules as replay.

    The event is judged at its own ``at`` (claims, leases and grants lapse by it), not at ``state.now``, which only
    shapes the views. It needs no ``host_sig`` yet. ``O(event)`` for a ticket event; a workspace event that changes
    every ticket (member, role, policy, addon, restore, compromised device) copies all tickets.
    """
    ctx = dataclasses.replace(state._ctx, admit=True)
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


def _after(state: State, core: Core, event: dict[str, Any], log: str, now: str | None) -> State:
    """The state for ``core`` (``state`` plus ``event`` applied), rebuilding only the views the event can change."""
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


def advance(state: State, event: dict[str, Any], *, log: str, now: str | None = None) -> State:
    """The state after the store appended ``event`` (as replay sees it: a refused event becomes an invalid one).

    Incremental: only the views the event can change are rebuilt (the touched ticket, or every ticket for a
    workspace event that can change them); the rest are shared with ``state``.
    """
    core = _fork(state._core)
    apply_event(core, log, event, state._ctx, commit=True, cow=True)
    return _after(state, core, event, log, now)


def preview(state: State, event: dict[str, Any], *, log: str) -> State | Refusal:
    """The state **if** ``event`` were appended next, or the refusal ``admit`` would give. For a caller that plans
    several events in a row (an atomic batch): each is judged on top of the ones before it, exactly as ``admit`` judges
    it, and nothing is written. ``event`` is stamped like an event about to be appended (``seq``, ``prev``, ``at``,
    ``ws_seq``, ``based_on``), without a ``host_sig``; ``prev`` of the next one is ``canon.event_head`` of this one."""
    ctx = dataclasses.replace(state._ctx, admit=True)
    core = _fork(state._core)
    r = apply_event(core, log, event, ctx, commit=True, cow=True)
    if r is not None:
        return r
    return _after(state, core, event, log, None)


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
