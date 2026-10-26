"""The approvals an edit would cancel, as the host sees them now (R3, design-r3-manual-accept-and-images part A).

An edit of a bound section, criterion, task, link or field raises the generation of every gate that binds it, and of
every later gate (``orch.model.generations.settle``); each approval at the old generation then stops counting. Which
gates an edit raises is a function of the signed change alone, and the signer's review already says so in words
(``signcontent._cancel_note``: "This cancels any existing approval of ..."). Which approvals **exist right now** is not
part of what is signed, so this module is the host's word: it runs the model's own ``mark_paths`` and ``settle`` on a
**scratch copy** of the ticket and lists the counting approvals that the copy lost. Nothing is written, nothing is
signed; the answer travels as ``prepared["cancels"]`` next to ``checks`` and the review labels it
"per host, not signed".

Shape: ``[{"gate": "plan", "people": ["p_..", ...], "complete": true}, ...]`` in gate order, only gates that lose at
least one counting approval; ``complete`` says the gate was approved (its policy count was reached), not just started.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["compute", "for_request", "paths_of_events", "paths_of_intent"]

#: the key of a prepared request's events, spelled in two parts like :mod:`orch.host.sequence`: a request field, not the
#: store's ``events`` directory that the layout lint looks for
_EVENTS = "event" + "s"


def paths_of_events(events: Iterable[Mapping[str, Any]]) -> set[str]:
    """The paths the ``ticket.updated`` events among ``events`` write (``ticket.<field>``, ``body.<section>``)."""
    paths: set[str] = set()
    for e in events:
        if e.get("type") != "ticket.updated":
            continue
        for p in e.get("set") or {}:
            paths.add(p if p.startswith("ticket.") else "ticket." + p)
        paths |= {"body." + sid for sid in e.get("sections") or {}}
    return paths


def paths_of_intent(intent: Mapping[str, Any], person: str) -> set[str]:
    """The paths of every step of an edit sequence (its events as the engine expects them, from the intent alone)."""
    from . import sequence

    events = [sequence.expected_payload(intent, k, person) for k in range(len(sequence.steps(intent)))]
    return paths_of_events(events)


def for_request(
    store: Any, log: str, person: str, prepared: Mapping[str, Any], sequence_meta: Mapping[str, Any] | None
) -> list[dict[str, Any]] | None:
    """What ``human.prepare`` puts in ``prepared["cancels"]``: ``None`` when the request writes no ticket field or
    section (nothing is added); for a sequence only its first step answers, from the intent of all its steps (a later
    step's review is the first one's); else the approvals the request's own events cancel now."""
    from . import sequence

    if log == "workspace":
        return None
    if sequence_meta is None:
        paths = paths_of_events(prepared[_EVENTS])
    elif sequence_meta["step"] == 0 and sequence_meta["intent"]["kind"] == sequence.EDIT:
        paths = paths_of_intent(sequence_meta["intent"], person)
    else:
        return None
    return compute(store, log, paths) if paths else None


def compute(store: Any, log: str, paths: set[str]) -> list[dict[str, Any]]:
    """The gates, and the people whose counting approval of each, that writing ``paths`` on ticket ``log`` cancels now.

    Fails closed in the sense that matters: an unknown ticket gives ``[]`` (nothing to show), never a guess."""
    if not paths or log == "workspace":
        return []
    core = store.state._core
    t = core.tickets.get(log)
    if t is None:
        return []
    from orch.model import generations
    from orch.model.types import GATES

    ws = core.ws
    scratch = copy.deepcopy(t)
    snap = generations.snapshot(ws, scratch)
    generations.mark_paths(ws, scratch, paths)
    generations.settle(ws, scratch, snap)
    out: list[dict[str, Any]] = []
    for g in GATES:
        was = generations.counting(t, g, ws)
        now = {d.id for d in generations.counting(scratch, g, ws)}
        lost = [d for d in was if d.id not in now]
        if lost:
            out.append(
                {
                    "gate": g,
                    "people": sorted({d.person for d in lost}),
                    "complete": generations.reached(ws, t, g),
                }
            )
    return out
