"""``relay.pair_device`` on the host (H12c-e, design 11A.15.10 item 12, 11A.15.6 step 9): the entry point R6c uses.

At step 9 the CLI attached to the offer (or ``orch sign CODE`` for a dashboard-created offer) drives the person path:

1. ``human.prepare {op:"relay.pair_device", args:{offer_id}, handoff:true}`` (or ``signer:{device, auth:"passphrase"}``
   for the attached CLI's inline signer). The host asks the workspace's :class:`PairDeviceSource` (R6c registers it
   as ``WorkspaceService.pair_device``) for the offer's verified facts (:meth:`PairDeviceSource.facts`), builds
   ``device.added {device, cert}`` and the material of kind ``relay.pair_device`` from them
   (:mod:`orch.identity.pairdevice`), admits the phone's pair declaration as its reader (``source:"pair"``) and judges
   the event with the model. ``auth:"convenience"`` is allowed (B10: WORD class, the typed word; the flag and the
   readers rule still apply through ``convenience.check`` and invariant I).
2. The terminal signer checks and renders the material (:func:`orch.host.signer.check`, :func:`render`), the person
   types the confirmation word (the device id's last eight hex), and the device key signs.
3. ``human.submit``. Under the store lock, in the one critical section of the append choke point
   (:func:`orch.host.human.append_signed`): :meth:`PairDeviceSource.precommit` (R6's (i) state and deadline check and
   (ii) the allocation intent), invariant I with the phone's declaration (iii) and its reader record (v), the append
   (iv), then :meth:`PairDeviceSource.committed` ((vi) ``devices/<id>.json``, (vii) the offer ``allocated``). A
   refusal after ``precommit`` (nothing appended) is :meth:`PairDeviceSource.refused`.

This module holds no key, signs nothing and keeps no pairing state: the offer records are R6's. Without a registered
source the op answers ``op.unsupported`` (no relay pairing in this build or for this workspace).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from orch import canon
from orch.identity import pairdevice

from .errors import HostError

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = [
    "OP",
    "PairAllocation",
    "PairDeviceSource",
    "check_entries",
    "event_digest",
    "facts_for",
    "hooks",
    "is_pair",
    "source_of",
]

_log = logging.getLogger(__name__)

OP = pairdevice.OP

#: the store stamps a log line with these; an appended event is the submitted one plus them
_STAMP = frozenset({"seq", "at", "prev", "ws_seq", "host_sig"})


@dataclass(frozen=True)
class PairAllocation:
    """What the append tells R6 about the event it is allocating (11A.15.6 step 9 (ii), the allocation intent):

    * ``offer_id``, ``attempt``, ``device``, ``cert_digest``, ``scopes``: the reviewed material's, exactly as the
      person saw them (bound by ``review_digest``); R6 writes ``devices/<id>.json`` with these ``scopes``, never its
      own copy;
    * ``review_digest``: ``sha256(cj(material))`` of the prepared request, the reviewed prepared material;
    * ``event_id``: the ``device.added`` event's id (a CSPRNG ULID: one event);
    * ``event_digest``: ``sha256:`` + hex of ``sha256(cj(event))`` of the **signed** event as submitted (with ``sig``,
      without the store's stamp ``seq``, ``at``, ``prev``, ``ws_seq``, ``host_sig``): recovery recomputes it from
      the log line by dropping those keys (:func:`event_digest`);
    * ``decl_digest``: the admitted declaration's digest (``rd``), or ``None`` (no declaration, level 0)."""

    offer_id: str
    attempt: str
    device: str
    cert_digest: str
    scopes: tuple[str, ...]
    review_digest: str
    event_id: str
    event_digest: str
    decl_digest: str | None


class PairDeviceSource(Protocol):
    """R6c's side of ``relay.pair_device`` (registered as ``WorkspaceService.pair_device``). Every method runs on the
    workspace's worker thread; ``precommit``, ``committed`` and ``refused`` run **under the store lock** (the one
    critical section of step 9) and must not take it again or block on the pair thread for long."""

    def facts(self, offer_id: str) -> pairdevice.PairDeviceFacts:
        """The verified facts of ``offer_id`` at step 9 (state ``confirmed``/``cert_pending`` with a certificate in
        hand, step 8; within the phone's deadline minus 120 s). Raise :class:`HostError` with R6's code otherwise
        (nothing is prepared)."""
        ...

    def precommit(self, allocation: PairAllocation) -> None:
        """Steps (i) and (ii): the offer is still at step 9 with this certificate and attempt, within its deadline;
        the allocation intent is written durably. Raise :class:`HostError` to refuse: nothing is appended (and
        :meth:`refused` is **not** called: the refusal is R6's own).

        **A repeat with an equal allocation must be accepted** (the host died between the intent and the append, and
        the start-up roll-forward runs this again): the same intent, already written, is not a conflict. Before it
        is called, the host has re-fetched :meth:`facts` and refused unless they rebuild the reviewed material
        exactly (``human.request_mismatch``)."""
        ...

    def committed(self, allocation: PairAllocation, seq: int) -> None:
        """Steps (vi) and (vii), after the append (the F1 line is the commit): ``membership/devices/<id>.json`` and
        the offer ``allocated``. Must be idempotent (a roll-forward calls it again). **R6 checks the log line too:**
        the ``device.added`` with ``event_id`` must have ``event_digest`` (:func:`event_digest` of the line) before it
        writes anything; the host already refuses a landed id of other content, and never calls this then. An
        exception is logged, never undoes the commit: R6's recovery at open matches the intent to the log line
        (``event_id``, ``event_digest``)."""
        ...

    def refused(self, allocation: PairAllocation, error: HostError) -> None:
        """Nothing was appended after :meth:`precommit` accepted (the reader gate, the model): R6 ends the offer
        ``failed`` (short ``rejected``)."""
        ...


def is_pair(op: Any) -> bool:
    return op == OP


def source_of(service: WorkspaceService) -> PairDeviceSource:
    source = getattr(service, "pair_device", None)
    if source is None:
        raise HostError("op.unsupported", "relay.pair_device needs the host's relay pairing (R6), not running here")
    return source


def facts_for(service: WorkspaceService, args: dict[str, Any]) -> pairdevice.PairDeviceFacts:
    """The facts of ``args.offer_id`` from the workspace's source, checked to be for this workspace and offer."""
    facts = source_of(service).facts(args["offer_id"])
    if not isinstance(facts, pairdevice.PairDeviceFacts):
        raise HostError("internal", "the relay pairing returned no pairing facts")
    if facts.workspace_id != service.workspace_id or facts.offer_id != args["offer_id"]:
        raise HostError("internal", "the relay pairing returned the facts of another workspace or offer")
    return facts


def check_entries(prepared: dict[str, Any], readers: list[dict[str, Any]]) -> None:
    """The admitted pair readers of a ``relay.pair_device`` request are bound to the reviewed offer and attempt
    (``bind`` equal to the material's) and name its device: checked at binding, at submit and at the append."""
    material = prepared["material"]
    want = {"via": "pair", "offer_id": material.get("offer_id"), "attempt": material.get("attempt")}
    for entry in readers:
        if entry.get("source") != "pair" or entry.get("bind") != want or entry.get("device") != material.get("device"):
            raise HostError("human.request_mismatch", "a pair declaration is bound to another offer, attempt or device")


def event_digest(event: dict[str, Any]) -> str:
    """``sha256:`` + hex of ``sha256(cj(event))`` without the store's stamp keys (a submitted or a logged event)."""
    body = {k: v for k, v in event.items() if k not in _STAMP}
    return "sha256:" + hashlib.sha256(canon.dumps(body)).hexdigest()


def allocation(record: dict[str, Any], event: dict[str, Any]) -> PairAllocation:
    prepared = record["prepared"]
    material = prepared["material"]
    entry = next((r for r in record.get("readers") or [] if r.get("device") == material["device"]), None)
    return PairAllocation(
        offer_id=material["offer_id"],
        attempt=material["attempt"],
        device=material["device"],
        cert_digest=material["cert_digest"],
        scopes=tuple(material["scopes"]),
        review_digest=prepared["review_digest"],
        event_id=event["id"],
        event_digest=event_digest(event),
        decl_digest=entry["digest"] if entry else None,
    )


@dataclass
class Hooks:
    """The three calls :func:`orch.host.human.append_signed` makes for a ``relay.pair_device`` request."""

    before: Callable[[list[dict[str, Any]]], HostError | None]
    after: Callable[[list[int]], None]
    failed: Callable[[HostError], None]


def hooks(service: WorkspaceService, record: dict[str, Any]) -> Hooks:
    """The source's step-9 calls for ``record``'s single ``device.added`` (under the store lock)."""
    event = record["submit"]["events"][0]
    alloc = allocation(record, event)
    state = {"precommitted": False, "mismatch": False}

    def before(new: list[dict[str, Any]]) -> HostError | None:
        if not new:  # a roll-forward of a landed event: the intent was written before it landed
            logged = service.store.logged_event("workspace", alloc.event_id)
            if logged is None or event_digest(logged) != alloc.event_digest:  # same id, other content: never ours
                error = HostError("human.request_mismatch", "an event with this id and other content is in the log")
                try:
                    source_of(service).refused(alloc, error)
                except Exception as exc:  # noqa: BLE001 - R6's deadline ends the offer anyway
                    _log.warning("relay.pair_device: refused raised %s", type(exc).__name__)
                state["mismatch"] = True
                return error
            return None
        try:
            check_entries(record["prepared"], record.get("readers") or [])
            source = source_of(service)
            current = facts_for(service, {"offer_id": alloc.offer_id})
            reviewed = {k: v for k, v in record["prepared"]["material"].items() if k != "review"}
            if canon.dumps(pairdevice.material(current)) != canon.dumps(reviewed):
                return HostError(
                    "human.request_mismatch", "the pairing changed since it was reviewed; prepare it again"
                )
            source.precommit(alloc)
        except HostError as exc:
            return exc
        except Exception as exc:  # noqa: BLE001 - any doubt refuses the append
            _log.warning("relay.pair_device: precommit raised %s; nothing appended", type(exc).__name__)
            return HostError("internal", f"the relay pairing could not record the allocation ({type(exc).__name__})")
        state["precommitted"] = True
        return None

    def after(seqs: list[int]) -> None:
        if state["mismatch"]:
            return
        try:
            source_of(service).committed(alloc, max(seqs))
        except Exception as exc:  # noqa: BLE001 - the F1 line is the commit; R6's recovery matches the intent
            _log.warning("relay.pair_device: committed raised %s; R6 recovers by the intent", type(exc).__name__)

    def failed(error: HostError) -> None:
        if not state["precommitted"]:
            return
        try:
            source_of(service).refused(alloc, error)
        except Exception as exc:  # noqa: BLE001 - R6's deadline ends the offer anyway
            _log.warning("relay.pair_device: refused raised %s", type(exc).__name__)

    return Hooks(before, after, failed)
