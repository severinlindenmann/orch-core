"""R1: the first-link sequence (contract 11A.5 "First link (R1)").

1. *(host, before this runs)* a worker job writes ``link.json`` with ``state: "linking"`` under the store lock, in the
   critical section of ``refuse_reader_op(history=True)``: the reader reservation of 11A.6. This module refuses to
   touch the network unless that reservation is in place.
2. *(owner client and person path, before this runs)* the owner's relay account and delegation (P3-D34) and the
   owner-signed genesis pin.
3. ``PUT card`` without a session (P3-D4). On a resume the relay already holds the card and answers 401; the same
   card is then PUT again under W(g) after step 4's hold.
4. challenge, login, ``host/hold``, ``PUT genesis`` (P3-D18; the relay refuses every checkpoint without it), the first
   publication with catch-up (R3, injected), then ``state: "linked"``.

Every step is idempotent on the relay, so a crash anywhere leaves ``linking`` and the next start runs the sequence
again from the top; the injected :class:`LinkSteps` returns the same persisted card and pin each time (a re-built card
at the same ``card_seq`` would be ``stale_card``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

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


def first_link(steps: LinkSteps, session: Session, directory: Directory, retrier: Retrier) -> None:
    state = steps.link_state()
    if state == "linked":
        return
    if state != "linking":
        raise LinkRefused("link.json is not in state linking: the reader reservation must come first")
    card = steps.card()
    seq = retrier.run(lambda: directory.put_card(card, first=True), route="PUT /v2/workspaces/{ws}/card")
    retrier.run(session.login, route="POST /v2/session/login")
    retrier.run(session.hold, route="POST /v2/workspaces/{ws}/host/hold")
    if seq is None:
        retrier.run(lambda: directory.put_card(card, first=False), route="PUT /v2/workspaces/{ws}/card")
    pin, cert, genesis = steps.genesis()
    retrier.run(lambda: directory.put_genesis(pin, cert, genesis=genesis), route="PUT /v2/workspaces/{ws}/genesis")
    steps.publish(session, directory)
    steps.mark_linked()
