"""R1: the first-link sequence (contract 11A.5 "First link (R1)").

1. *(host, before this runs)* a worker job writes ``link.json`` with ``state: "linking"`` under the store lock, in the
   critical section of ``refuse_reader_op(history=True)``: the reader reservation of 11A.6. This module refuses to
   touch the network unless that reservation is in place.
2. *(owner client and person path, before this runs)* the owner's relay account and delegation (P3-D34) and the
   owner-signed genesis pin.
3. ``PUT card`` without a session (P3-D4). On a resume the relay already holds the card and answers 401; the same
   card is then PUT again under W(g) after step 4's hold.
   Before it, and before any other request, the ``link.json`` state and :meth:`Session.preflight` (round 6).
4. challenge, login, ``host/hold``, ``PUT genesis`` (P3-D18; the relay refuses every checkpoint without it), the first
   publication with catch-up (R3, injected), then ``state: "linked"``.

Every step is idempotent on the relay, so a crash anywhere leaves ``linking`` and the next start runs the sequence
again from the top.

**Same bytes on resume (review L1).** The relay compares the whole genesis pin, signature included (a re-signed pin is a
permanent 409 ``genesis_mismatch``), and a rebuilt card at the same ``card_seq`` is ``stale_card``. So the card and the
pin are persisted in the relay state directory (``link-card.json``, ``link-genesis.json``, write-once) **before** their
first PUT, and a resume re-sends the stored bytes without asking :class:`LinkSteps` again: the person is never asked
to sign a second pin, and a different card or pin from ``LinkSteps`` is never sent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from orch import canon
from orch.wsproto import wire
from orch.wsproto.state import PersistedOnce, StateDir

from .directory import Directory
from .retry import Retrier
from .session import Session

__all__ = ["LinkRefused", "LinkSteps", "first_link"]


class LinkRefused(Exception):
    """The first link cannot start (no reservation, or an unknown link state)."""


class LinkSteps(Protocol):
    """What the host provides to the first link (R5/R6/R3 implement it; tests fake it)."""

    def link_state(self) -> str:
        """``"linking"`` or ``"linked"`` from ``link.json``."""
        ...

    def card(self) -> Mapping[str, Any]:
        """The full card to publish, persisted before its first PUT and identical on every call."""
        ...

    def genesis(self) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        """``(pin, cert, genesis_hash)``: the owner-signed genesis pin from the person path and its certificate."""
        ...

    def publish(self, session: Session, directory: Directory) -> None:
        """The first publication, with catch-up (R3)."""
        ...

    def mark_linked(self) -> None:
        """``link.json`` ``state: "linked"``, written durably."""
        ...


def _kept(store: StateDir, name: str, make: Callable[[], Any]) -> Any:
    """The persisted value of ``name``, or ``make()`` persisted durably first."""
    once = PersistedOnce(store, name)
    held = once.get()
    if held is None:
        held = once.keep(canon.dumps(make()))
    try:
        obj = wire.loads_canonical(held)
    except wire.WireError:
        raise LinkRefused(f"{name} is not readable; refusing to rebuild it") from None
    if type(obj) is not dict:  # null, a list or a number: refused, never a TypeError later (round 9)
        raise LinkRefused(f"{name} is not a JSON object; refusing to rebuild it")
    return obj


def first_link(steps: LinkSteps, session: Session, directory: Directory, retrier: Retrier, *, store: StateDir) -> None:
    """Run (or resume) the first link. Every local check comes before the first request (round 6, orch-52 #1): the
    ``link.json`` state, then :meth:`Session.preflight` (instance binding, ``pending``, (a), (b)), so a copied or
    restored ``linking`` directory sends nothing, not even the session-less ``PUT card``."""
    state = steps.link_state()
    if state == "linked":
        return
    if state != "linking":
        raise LinkRefused("link.json is not in state linking: the reader reservation must come first")
    session.preflight()
    card = _kept(store, "link-card.json", lambda: dict(steps.card()))
    seq = retrier.run(lambda: directory.put_card(card, first=True), route="PUT /v2/workspaces/{ws}/card")
    retrier.run(session.login, route="POST /v2/session/login")
    retrier.run(session.hold, route="POST /v2/workspaces/{ws}/host/hold")
    if seq is None:
        retrier.run(lambda: directory.put_card(card, first=False), route="PUT /v2/workspaces/{ws}/card")

    def make_genesis() -> dict[str, Any]:
        pin, cert, genesis = steps.genesis()
        return {"pin": dict(pin), "cert": dict(cert), "genesis": genesis}

    g = _kept(store, "link-genesis.json", make_genesis)
    pin, cert, genesis = g["pin"], g["cert"], g["genesis"]
    retrier.run(lambda: directory.put_genesis(pin, cert, genesis=genesis), route="PUT /v2/workspaces/{ws}/genesis")
    steps.publish(session, directory)
    steps.mark_linked()
