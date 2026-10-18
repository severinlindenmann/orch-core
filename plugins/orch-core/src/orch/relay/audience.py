"""The audience function (orch-v2-host 11A.16.2, decision D4: the intersection, everywhere), push form.

:func:`devices_for` is the ONE function that decides which devices something may cross to. H13a (push) builds the
push form here; H13b (Drop) adds the object form to the same function (its recipient and binding steps), it does not
fork it. A device passes only if **all** of these hold:

1. **Standing.** It is in the accepted member list with ``look`` in both its list entry and its certificate, and the F1
   standing function holds for it (``standing``: the one implementation of #554, ``device_standing_ok``, handed in by
   the host because ``orch.relay`` may not import the store module that defines it).
2. **Ticket.** The push is bound to no ticket, or the ticket is visible to the device's person (F1 §10.7,
   :func:`orch.ops.runtime.visible_to`).
3. (Drop: the object's recipient restriction. Not part of the push form.)
4. **Narrowing.** For a push context: the device belongs to one of the addressee persons (``narrow``), and is not an
   excluded device (the asking or answering device).
5. (Drop: the object's binding is known. Not part of the push form.)

There is no owner bypass and no OR of these conditions. It is called under the store lock (it reads one ``State`` and
one member list, no I/O). A test pins by AST that no other module of ``orch.relay`` builds a device set from the
member list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orch.ops.runtime import visible_to

__all__ = ["AudienceError", "PushContext", "devices_for"]

Standing = Callable[[Any, str, str], bool]


class AudienceError(Exception):
    """An object form this function does not know (the Drop form is H13b's)."""


@dataclass(frozen=True)
class PushContext:
    """The push form of D4: ``ticket`` (a ticket uid, or ``None`` for workspace-wide), ``narrow`` (the addressee
    persons as ``p_<hex>``, or ``None`` for everyone who passes the other steps) and ``exclude`` (device ids, 32 hex,
    that never get this push: the asking or answering device)."""

    ticket: str | None = None
    narrow: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()


def devices_for(state: Any, members: Any, obj: object, *, standing: Standing) -> list[str]:
    """The device ids (32 lower-case hex, sorted) in the audience of ``obj`` against ``state`` and the accepted
    ``members`` list (``None``: no accepted list yet, nobody)."""
    if not isinstance(obj, PushContext):
        raise AudienceError("only the push form is built (the Drop form is H13b)")
    if members is None:
        return []
    view = None
    if obj.ticket is not None:
        view = state.tickets.get(obj.ticket)
        if view is None:
            return []  # an unknown ticket is visible to nobody
    out = []
    for device_id, m in members.members.items():
        if device_id in obj.exclude:
            continue
        person = "p_" + m.person_id
        if "look" not in m.scopes or not _cert_look(m.cert):
            continue
        if not standing(state, "d_" + device_id, person):
            continue
        if view is not None and not visible_to(person, view):
            continue
        if obj.narrow is not None and person not in obj.narrow:
            continue
        out.append(device_id)
    return sorted(out)


def _cert_look(cert: Any) -> bool:
    o = cert.get("o") if hasattr(cert, "get") else None
    scopes = o.get("scopes_max") if hasattr(o, "get") else None
    return type(scopes) is list and "look" in scopes
