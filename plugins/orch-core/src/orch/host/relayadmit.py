"""The ONE adapter seam between R2's ``relay.submit`` (``orch/host/relayentry.py``) and core's H12e admission helpers.

H12e (orch-52, PR #474: ``orch.host.admission`` and ``orch.host.human.judge_events``) is not merged yet. Until it is,
this module holds R2's own stand-ins under H12e's names and signatures, and ``relayentry`` calls NOTHING else for the
steps of 11A.14 that read core state. The swap, when #474 lands, is this module's body becoming::

    from .admission import COLUMN_R_AUTH, check_target, check_window, known_or_fresh, next_key, signed_digest
    from .human import judge_events

``tests/relay/test_relayadmit_seam.py`` pins the contract the swap must keep (the names, the signatures, and the
behaviour R2 relies on: window edges, dedup by signed digest and actor, the key rule with ``detail["next_key"]``);
when ``orch.host.admission`` exists it also checks the real helpers against the same contract.

Known differences of the stand-ins (none weakens a check R2 relies on; the store judges every appended event again):

* :func:`known_or_fresh` never raises ``event.abandoned`` (that needs the replay core's abandoned ids, which is
  H12e's): until the swap the code is reserved (``framing.RETRY_NOTES``).
* :func:`check_target` has no visibility filter for ``question.answered`` (H12e's ``not_found`` for an invisible
  ticket); ``judge_events`` refuses an answer the person may not give.
"""

from __future__ import annotations

import hashlib
from typing import Any

from orch import canon

from .errors import HostError

__all__ = [
    "COLUMN_R_AUTH",
    "check_target",
    "check_window",
    "judge_events",
    "known_or_fresh",
    "next_key",
    "signed_digest",
]

#: P5: the only ``auth`` values column R accepts (11A.14); ``convenience`` is never one of them
COLUMN_R_AUTH = frozenset({"secure-enclave-unlocked", "passphrase"})
_PAST_MS, _FUTURE_MS = 900_000, 300_000
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid_ms(event_id: str) -> int:
    if type(event_id) is not str or len(event_id) != 26 or any(c not in _CROCKFORD for c in event_id):
        raise HostError("invalid.input", "the event id is not a ULID")
    n = 0
    for c in event_id[:10]:
        n = n * 32 + _CROCKFORD.index(c)
    return n


def check_window(event_id: str, now: float) -> None:
    """``event.expired`` unless the id's ULID time lies in [``now`` - 900 s, ``now`` + 300 s] (edges included);
    ``now`` is the HOST clock at job time, in epoch seconds. Only for a never-seen event (ruling A)."""
    t, now_ms = _ulid_ms(event_id), int(now * 1000)
    if not now_ms - _PAST_MS <= t <= now_ms + _FUTURE_MS:
        raise HostError("event.expired", "the event id is outside the admission window; review again")


def signed_digest(workspace_id: str, log: str, event: dict[str, Any]) -> str:
    """``"sha256:" + hex(sha256(person_signing_bytes(ws, log, event)))``: the signed content, never the signature."""
    return "sha256:" + hashlib.sha256(canon.person_signing_bytes(workspace_id, log, event)).hexdigest()


def _same(workspace_id: str, log: str, logged: dict[str, Any], event: dict[str, Any]) -> bool:
    try:
        return logged.get("actor") == event.get("actor") and signed_digest(workspace_id, log, logged) == signed_digest(
            workspace_id, log, event
        )
    except Exception:  # noqa: BLE001 - unbuildable bytes are not the same event
        return False


def known_or_fresh(store: Any, log: str, event: dict[str, Any], now: float) -> int | None:
    """L3 then the window (ruling A): the ``seq`` of an identical landed event (signed digest and actor), else
    ``event.duplicate_id`` for a known id with other content or actor; only a never-seen id is window-checked
    (``event.expired``). ``None``: never seen and fresh. Call inside the store-lock span."""
    with store.locked():
        found = store.logged_event(log, event["id"])
        if found is not None:
            if not _same(store.state.workspace.workspace_id, log, found, event):
                raise HostError("event.duplicate_id", "an event with this id and other content is in the log")
            return int(found["seq"])
    check_window(event["id"], now)
    return None


def next_key(store: Any) -> str:
    """The key the store allocates next (advisory: never a reservation)."""
    return store.peek_key()


def check_target(store: Any, log: str, event: dict[str, Any]) -> None:
    """L4/L5 for a submitted ``ticket.created`` or ``question.answered`` of ``log``: an answer's ticket exists
    (``not_found``); a creation's uid is unused (``ticket.exists``), its owner is the signer's person
    (``invalid.input``) and its key is exactly :func:`next_key` (``ticket.key_not_next`` with
    ``detail = {"next_key": ...}``)."""
    with store.locked():
        exists = store.log_head(log) is not None or store.ticket(log, heal=False) is not None
        if event.get("type") != "ticket.created":
            if not exists:
                raise HostError("not_found", "no such ticket")
            return
        if exists:
            raise HostError("ticket.exists", "a ticket with this uid exists")
        actor = event.get("actor") or {}
        if event.get("owner") != actor.get("id"):
            raise HostError("invalid.input", "the owner of a phone-created ticket is its signer")
        want = next_key(store)
        if event.get("key") != want:
            err = HostError("ticket.key_not_next", "the key is not the next free key")
            err.detail = {"next_key": want}  # type: ignore[attr-defined] - H12e's HostError.detail
            raise err


def judge_events(
    store: Any, log: str, events: list[dict[str, Any]], *, git: Any = None, copies: list[dict[str, Any]] | None = None
) -> None:
    """All events together with ``model.preview`` under the store lock (signatures verified), as ``HumanOps._judge``
    does for the person path, without a request record; raises the model's code, appends nothing."""
    from orch.model import Refusal as ModelRefusal
    from orch.model import preview
    from orch.store import StoreError

    del git, copies  # H12e's parameters; column R carries no git copies (one event, no extras, 11A.5 J5)
    with store.locked():
        state = store.state
        stamp = store.peek_stamp(log)
        seq, prev, at = stamp["seq"], stamp["prev"], stamp["at"]
        for ev in events:
            probe = {**ev, "seq": seq, "prev": prev, "at": at}
            if "ws_seq" in stamp:
                probe["ws_seq"] = stamp["ws_seq"]
            try:
                r = preview(state, probe, log=log)
            except Exception:  # noqa: BLE001 - device-supplied fields the model cannot judge: refused, never run
                raise HostError("invalid.input", "the event cannot be judged") from None
            if isinstance(r, ModelRefusal):
                exc = StoreError.from_refusal(r)
                raise HostError(exc.code, exc.detail or exc.code)
            state, seq, prev = r, seq + 1, canon.event_head(probe)
