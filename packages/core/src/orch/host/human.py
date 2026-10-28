"""The human contract on the host (H5a, §0.8.1): ``human.prepare``, ``human.get``, ``human.submit``, ``human.cancel``.

The host never prompts and holds no device key (D-P2-15). A person op is prepared here, signed by a signing client
(:mod:`orch.host.signer`) and submitted:

* **prepare** runs the inner op's own handler as the mapped person with a :class:`orch.ops.prepare.Capture`: every
  event it would sign or append is judged by the model and recorded instead. The host then builds the envelopes (event
  ids from a CSPRNG, one shared ``based_on``: the log head now), the ``material`` the signer reviews, its
  ``review_digest`` and the fingerprint (:mod:`orch.host.review`), and stores the request (``dry_run``: nothing is
  stored, ``request: null``). With ``opts.idem`` (v1.5) the same principal, idem and arguments give the same
  request in its current state; other arguments are ``idem.conflict``; the outcome store keeps the record 24 h.
* **get** returns the request; with ``signer`` it binds a ``prepared`` request to that device (device checks, then
  ``actor.device`` and ``auth`` are filled in, once). Nothing else of the request ever changes (invariant bytes).
* **submit** is a log write: the generic idempotency step comes first (§0.6 step 9, H2b's outcome store), then the
  request-level rules in the order of §0.8.1. The signed events must equal the bound ones except ``sig`` and the
  ``secret_hash`` of a ``grant.issued``; all are judged together under one store lock, the signed events are written
  to the request (fsync), and they are appended one by one under that lock. A crash between two appends is rolled
  forward at start (:func:`recover`), so a human submission is never ``partial``.
* **cancel** ends a pending request.

* **pending** lists the caller's requests (or the one named by ``request_id``), each with ``error: {code}`` after a
  refused submit (the catalog code only).

Unbound requests are reserved for hub-mediated operations: ``human.prepare {handoff: true}`` (no signer) stores one only
for :data:`HUB_HANDOFF_OPS` (the add-on grant the hub answers, FB15); every other request is bound at prepare (a dry
run stores nothing either way). No terminal code is issued.
"""

from __future__ import annotations

import base64
import copy
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch import canon
from orch.addons.settings_value import KEY as ADDONS_KEY

from . import convenience, hubproof, marketinstall, review
from .errors import HostError
from .outcomes import Planned, request_idem
from .requests import (
    EXPIRE_SECONDS,
    GRANT_EXPIRE_SECONDS,
    KEEP_SECONDS,
    PENDING,
    RequestStore,
    file_bytes,
    new_request_id,
)
from .workspace import ok_result

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .auth import Redactor
    from .workspace import Call, WorkspaceService

__all__ = ["HUMAN_OPS", "HumanOps", "judge_events", "recover"]

HUMAN_OPS: dict[str, int] = {
    "human.prepare": 1,
    "human.get": 1,
    "human.submit": 1,
    "human.cancel": 1,
    "human.pending": 1,
}
PHASES = {
    "human.prepare": "read",
    "human.get": "read",
    "human.submit": "write",
    "human.cancel": "read",
    "human.pending": "read",
}

_DEVICE = {"type": "string", "pattern": r"^d_[0-9a-f]{32}$"}
_SIGNER = {
    "type": "object",
    "properties": {"device": _DEVICE, "auth": {"enum": ["passphrase", "secure-enclave", "convenience"]}},
    "required": ["device", "auth"],
    "additionalProperties": False,
}
_REQ = {"type": "string", "pattern": r"^hr_[0-7][0-9A-HJKMNP-TV-Z]{25}$"}
_SCHEMAS = {
    "human.prepare": {
        "type": "object",
        "properties": {
            "op": {"type": "string", "minLength": 1, "maxLength": 64},
            "args": {"type": "object"},
            "signer": _SIGNER,
            # FB15: true (without signer) stores an unbound request for HUB_HANDOFF_OPS only (_check_binding)
            "handoff": {"type": "boolean"},
            "sequence": {"type": "object"},
        },
        "required": ["op", "args"],
        "additionalProperties": False,
    },
    "human.get": {
        "type": "object",
        "properties": {"request_id": _REQ, "signer": _SIGNER, "owner_proof": {"type": ["object", "null"]}},
        "required": ["request_id"],
        "additionalProperties": False,
    },
    "human.submit": {
        "type": "object",
        "properties": {
            "request_id": _REQ,
            "events": {"type": "array", "minItems": 1, "maxItems": 16, "items": {"type": "object"}},
        },
        "required": ["request_id", "events"],
        "additionalProperties": False,
    },
    "human.cancel": {
        "type": "object",
        "properties": {"request_id": _REQ},
        "required": ["request_id"],
        "additionalProperties": False,
    },
    "human.pending": {
        "type": "object",
        "properties": {"request_id": _REQ},
        "maxProperties": 1,
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_GATE_TYPES = {"gate.approved", "gate.changes_requested", "verdict.given"}
_HASH = r"sha256:[0-9a-f]{64}"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def _submitted_at(record: dict[str, Any]) -> str | None:
    """The host's time when ``human.submit`` was accepted for the request; ``None`` until then (and for a record
    stored before the field existed)."""
    at = record.get("submitted_at")
    return _iso(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def _expired(record: dict[str, Any], when: str = "") -> str:
    """The expiry message with the request's actual lifetime (10 minutes; a grant's: 4)."""
    minutes = round((record["expires"] - record["created"]) / 60)
    return f"the request expired{' ' + when if when else ''}: it lives {minutes} minutes after prepare"


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def _tail_id(store: Any, log: str) -> str | None:
    """The id of the last event of ``log`` (``None`` for an empty or unknown log): what a sequence's chain compares."""
    nxt = int(store.peek_stamp(log)["seq"])
    if log == "workspace":  # the workspace log is read by events_after (events() takes a ticket)
        tail = [e for e, _ in store.events_after(log, max(nxt - 2, 0), 2)]
    else:
        tail = store.events(log, after=max(nxt - 2, 0), limit=2)
    return str(tail[-1]["id"]) if tail and nxt > 1 else None


def _store_error(exc: Any) -> HostError:
    return HostError(exc.code, exc.detail or exc.code)


def _unsigned(event: dict[str, Any], *, grant: bool) -> dict[str, Any]:
    out = {k: v for k, v in event.items() if k != "sig"}
    if grant and out.get("type") == "grant.issued":
        out.pop("secret_hash", None)
    if grant and out.get("type") in review.OWNER_PROOF_TYPES:
        out.pop("owner_proof", None)  # the signer fills it after the review; the submit checks it (hubproof)
    return out


# --- devices ------------------------------------------------------------------------------------------------------


def _device_refusal(dev: Any, now: float, operate: bool) -> tuple[str, str] | None:
    """F1 device rules on the replayed roster view (``orch.model.authz.device_valid``'s, as codes)."""
    if dev.removed or dev.revoked:
        return "device.invalid", "the device was removed or revoked"
    if dev.expires_ms is not None and dev.expires_ms <= int(now) * 1000:
        return "device.invalid", "the device certificate has expired"
    scopes = dev.scopes
    if (
        any(str(s).startswith("drop:") for s in scopes)
        or "decide" not in scopes
        or (operate and "operate" not in scopes)
    ):
        return "device.scope", "the certificate lacks the scope this request needs"
    return None


def check_device(store: Any, person: str, device: str, now: float, *, operate: bool) -> None:
    """R7: the device belongs to ``person`` and has a valid certificate with ``decide`` (and ``operate`` where an
    event needs it): ``device.unknown``, ``device.invalid``, ``device.scope``."""
    dev = store.state.workspace.devices.get(device)
    if dev is None or dev.person != person:
        raise HostError("device.unknown", f"{device} is not a device of {person}")
    refusal = _device_refusal(dev, now, operate)
    if refusal is not None:
        raise HostError(*refusal)


def probe_device(store: Any, person: str, now: float) -> str:
    """A valid ``decide`` device of ``person`` the model judges unsigned events under (a hand-off request)."""
    for ref, dev in sorted(store.state.workspace.devices.items()):
        if dev.person == person and _device_refusal(dev, now, False) is None:
            return ref
    raise HostError("device.invalid", f"{person} has no valid device")


#: #1097: the one op whose signer is a device the roster does not know yet (the NEW device signs its own
#: ``device.rotated``); the model judges that shape (``breakglass.is_rotation``), not the roster check below.
ROTATE_OP = "device.rotate"


def _removing(op: str, devices: list[Any]) -> frozenset[str]:
    """#1198 C15: the devices a ``device.remove`` takes away, for the readers condition (judged after the removal)."""
    return frozenset(d for d in devices if op == "device.remove" and isinstance(d, str))


def _operate(types: list[str]) -> bool:
    from orch.model.authz import OPERATE

    return any(t in OPERATE for t in types)


# --- the ops ------------------------------------------------------------------------------------------------------


def _person_operation(name: str) -> Any:
    """The operation ``human.prepare`` runs for ``name``: a registry op, or one of the host person ops
    (move, policy.set, settings.set), which are not registry (CLI) ops. Prepare and its idem binding share this."""
    if name in review.HOST_PERSON_OPS:
        from orch.ops import person_ops

        return person_ops.get(name)
    import orch.ops as ops

    return ops.get(name)


#: the first line of a dry run's result in both modes (``orch.ops.runtime.Call.result``)
DRY_RUN_NOTE = "dry-run: nothing was written"

#: FB15: the only ops ``human.prepare {handoff: true}`` stores, unbound, for the hub to answer (``hub_handoff``)
HUB_HANDOFF_OPS = frozenset({"addon.grant"})


def _check_binding(name: str, signer: Any, handoff: bool, dry_run: bool) -> None:
    """``human.prepare`` takes ``signer`` (bound at prepare), or ``handoff: true`` without a signer for a hub hand-off
    op only (:data:`HUB_HANDOFF_OPS`): every other request is bound at prepare. A dry run binds and stores nothing: it
    may take neither (the dashboard's folder check), or a hand-off of any op, never both."""
    if handoff and signer is not None:
        raise HostError("invalid.input", "human.prepare takes signer, or handoff:true without a signer")
    if dry_run:
        return
    if handoff and name not in HUB_HANDOFF_OPS:
        raise HostError(
            "invalid.input",
            f"human.prepare: handoff:true is for the hub's add-on grant only, not {name}; prepare it with a signer",
        )
    if not handoff and signer is None:
        raise HostError("invalid.input", "human.prepare takes signer, or handoff:true without a signer")


class HumanOps:
    """``human.*`` of one request, on the workspace worker thread (the dispatcher calls :meth:`serve`)."""

    ops = HUMAN_OPS

    def __init__(self, backend: Any) -> None:
        self.backend = backend  # the RegistryBackend: its context, file and state helpers

    def serve(self, call: Call, redactor: Redactor) -> dict[str, Any]:
        op = call.frame["op"]
        args = call.frame["args"]
        _validate(op, args)
        if op == "human.submit":
            return self.submit(call, args, redactor)
        request_idem(op, call.frame.get("opts"))  # dry_run on get or cancel is invalid.input; idem is ignored
        dry_run = bool((call.frame.get("opts") or {}).get("dry_run"))
        if call.frame.get("files") and op != "human.prepare":
            raise HostError("blob.invalid", f"{op} takes no files")
        if op == "human.prepare":
            return ok_result(self.prepare(call, args, dry_run))
        self.backend._state(call.service, False)
        requests = self._requests(call.service)
        if op == "human.get":
            return ok_result(self.get(call, requests, args))
        if op == "human.pending":
            return ok_result(self.pending(call, requests, args))
        return ok_result(self.cancel(call, requests, args))

    @staticmethod
    def _requests(service: WorkspaceService) -> RequestStore:
        if service.requests is None:
            raise HostError("workspace.unavailable", "the workspace cannot be served: no request store")
        return service.requests

    # -- prepare
    def prepare(self, call: Call, args: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        service = call.service
        name = args["op"]
        types = review.person_op_types(name)
        from .dispatch import NOT_SERVED

        if types is None or name in NOT_SERVED:
            raise HostError("op.not_allowed", f"{name} is not an op a person runs through human.prepare")
        _check_binding(name, args.get("signer"), bool(args.get("handoff", False)), dry_run)
        self.backend._state(service, True)  # the request prepares a write
        intent: Any = None
        idem = (call.frame.get("opts") or {}).get("idem")
        if idem is not None and not dry_run:
            # v1.5: prepare honours opts.idem (§0.7). The idem step comes before the quota, so a retry at 20 pending
            # still finds its request. Nothing is stored for a dry run.
            same, intent = self._begin_idem(call, str(idem), args)
            if same is not None:
                return same  # the same request, in its current state
        try:
            return self._prepare(call, args, dry_run, intent)
        except BaseException:
            if intent is not None:
                rid = intent.record.get("request_id")
                try:
                    stored = bool(rid) and self._requests(service).read(rid) is not None
                except Exception:  # noqa: BLE001 - unreadable: keep the record, never hide the original error
                    stored = True
                if stored:
                    # the request is stored but the answer was not: keep the record (with the request id), so the
                    # retry finds this request instead of creating a second one
                    service.outcomes.release(intent)
                else:
                    service.outcomes.abandon(intent)  # nothing was created: a retry runs again
            raise

    def _begin_idem(self, call: Call, idem: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, Any]:
        """The outcome-store step of ``human.prepare {idem}``: ``(None, intent)`` for a fresh record, or
        ``(view, None)`` for the request the idem names, as it is now. Other arguments are ``idem.conflict``: the
        binding holds the op, the inner args, the signer or hand-off, the session and the file digests."""
        service = call.service
        name = args["op"]
        operation = _person_operation(name)
        inner = dict(args["args"])
        specs = self._specs(operation, call.frame, inner)
        # a {blob} must be the preparing principal's (F9); a {digest} binds only to an existing record (§0.3)
        digests, digest_args = self.backend.bind_files(call, idem, specs)
        # the inner args as given (not normalised: a REF and its uid are two bindings, which only ever errs towards
        # idem.conflict), the person and the signer or hand-off; the session and file digests are added by binding_of
        bound = {
            "op": name,
            "args": inner,
            "signer": args.get("signer"),
            "handoff": bool(args.get("handoff", False)),
            "person": call.viewer,
            "sequence": args.get("sequence"),
        }
        session = (call.frame.get("auth") or {}).get("session")
        outcomes = service.outcomes
        begun = outcomes.begin(
            call.principal,
            idem,
            "human.prepare",
            bound,
            planned=(),
            files=digests,
            session=session,
            deferred=True,
            digest_args=digest_args,  # a {digest} where the prepare has to run again: upload again
        )
        if isinstance(begun, dict):  # a stored answer: the prepare result of the first call
            begun = {k: v for k, v in begun.items() if k != "duplicate"}
            requests = self._requests(service)
            record = requests.read(begun["request"]["id"])
            if record is None or record.get("principal") != call.principal:
                raise HostError("idem.expired", "the request this idem prepared is no longer kept; use a new idem")
            return self._view(requests.expire(record)), None
        return None, begun

    def _prepare(self, call: Call, args: dict[str, Any], dry_run: bool, intent: Any) -> dict[str, Any]:
        from orch.ops.errors import OrchError
        from orch.ops.prepare import Capture
        from orch.store import StoreError

        service = call.service
        name = args["op"]
        types = review.person_op_types(name)
        assert types is not None
        signer, handoff = args.get("signer"), bool(args.get("handoff", False))
        from .dispatch import host_error, result_dict

        if not dry_run:
            self._requests(service).admit(call.viewer)  # type: ignore[arg-type]
        store = service.store
        person = call.viewer
        assert person is not None  # column P only
        now = service.clock()
        store.refresh()
        if name != "member.add":  # after the handler, with the reader's declaration (H12d-2)
            convenience.refuse_reader_op(service, name)  # §0.8.6: no new reader while convenience signing is on
        # #1198 C10: a One-click signer may prepare a hub step: the owner proof comes from the backup key sealed to it.
        # Nothing is signed or submitted without the proof: ``get`` refuses a convenience bind without one (below), and
        # the submit and the model check every hub event under the lock (hubproof).
        if signer is not None and signer["auth"] == "convenience":
            convenience.check(
                service,
                person,
                signer["device"],
                removing=_removing(name, [args["args"].get("device")]),
                leaving=name == "hub.leave",
            )  # the owner's condition, at prepare
        if signer is not None and name != ROTATE_OP:
            check_device(store, person, signer["device"], now, operate=_operate(sorted(types)))
        operation = _person_operation(name)
        inner = dict(args["args"])
        from orch.ops.inputs import check_input

        try:
            check_input(operation, inner)
        except OrchError as e:
            raise host_error(e, operation) from None
        specs = self._specs(operation, call.frame, inner)
        self.backend.bind_files(call, None, specs)  # a {blob} must be a complete blob of the preparing principal (F9)
        files = self.backend._file_bytes(call, specs, inner)
        capture = Capture(
            person,
            signer["device"] if signer else None,
            signer["auth"] if signer else None,
            signer["device"] if signer else probe_device(store, person, now),
            dry_run=dry_run,
        )
        capture.dry_run = dry_run
        capture.hub_handoff = name in HUB_HANDOFF_OPS and handoff and signer is None  # FB15
        capture.market_files = marketinstall.files_of(service)
        if args.get("sequence") is not None and name == "artifact.add":
            self._check_pasted_image(inner, files)
        meta = self._sequence(call, name, inner, args, person, store, dry_run)
        capture.sequence = meta
        if name == "publish.share":  # P5: the staged copy's own words (the args name only the staged id)
            from . import publishing

            capture.publish = publishing.facts_for(service, inner, call)
        if name in ("hub.join", "hub.mode"):  # V3 C2a: the host's clock and dev state, never the request's
            from . import hubops

            capture.hub = hubops.facts_for(service)
            convenience.refuse_hub_reader(
                service, name, inner.get("hub_card"), now_ms=capture.hub.now_ms, dev_mode=capture.hub.dev_mode
            )  # #1198 C9: the hub's card declares a reader level the log's history needs
        ctx = self.backend._context(call, None, False, files, prepare=capture)
        try:
            res = operation.handler(ctx, inner)
        except OrchError as e:
            raise host_error(e, operation) from None
        except StoreError as e:
            raise _store_error(e) from None
        if not capture.events:
            raise HostError("invalid.input", f"{name} has nothing to sign")
        if name not in ("ac.accept", "ac.unaccept") and any(
            isinstance(c.event.get("label"), str) and c.event["label"].startswith(("accepted ", "withdrawn "))
            for c in capture.events
        ):  # R3: only the acceptance ops write the labels that mean "accepted manually" / "withdrawn"
            raise HostError("invalid.input", f"{name} cannot write a label that means a manual acceptance")
        logs = {c.log for c in capture.events}
        if len(logs) != 1:
            raise HostError("internal", f"{name} prepared events for more than one log")
        log = logs.pop()
        if signer is not None and name != ROTATE_OP:
            check_device(
                store, person, signer["device"], now, operate=_operate([c.event["type"] for c in capture.events])
            )
        prepared, extras = self._build(call, name, log, capture, store, now)
        if name in hubproof.OPS:  # #1198 C9: what the signer needs to make the owner proof (the host's own dev state)
            from . import hubops

            prepared["owner_proof"] = {"dev": bool(hubops.workspace_dev(service))}
        # H12d-2 (§0.8.6 item 9.4): the new reader's declaration against required_reader, early, with a good message
        built = prepared["events"]
        convenience.refuse_reader_op(service, name, events=built, readers=capture.readers)
        # FB15: an add-on grant may be PREPARED as a hub hand-off (signer None, an optional extra path): nothing is
        # signed here. The hub's channel signs an ``addon.requested`` that names this request, and the host answers it
        # (hubdeliver). The local signature path is always open (A1, D83).
        if capture.checks:  # N3: the clean-tree checks of a decision that binds a commit (not signed)
            prepared["checks"] = capture.checks
        from . import cancels as cancel_preview

        cancels = cancel_preview.for_request(store, log, person, prepared, capture.sequence)
        if cancels is not None:  # R3: the approvals this edit cancels right now: the host's word, not signed
            prepared["cancels"] = cancels
        if not dry_run:  # a dry run runs no tree.check job (section 0.8.1) and carries no checks
            # a decision that binds commits is never prepared without its checks
            require_checks(call, prepared, capture.copies)
        result = result_dict(res)
        # H7: the lines too (a grant's "the secret was shown" line), so the committed result renders as direct mode's
        template = {"data": result["data"], "key": result["key"], "hints": result["hints"], "lines": result["lines"]}
        if dry_run:  # H7: the inner op's would-be result, as direct mode's --dry-run answers it (CLI parity)
            would = {**result, "lines": list(result["lines"])}
            if would["lines"][:1] != [DRY_RUN_NOTE]:
                would["lines"].insert(0, DRY_RUN_NOTE)
            return {"request": None, **prepared, "result": would}
        rid = new_request_id()
        requests = self._requests(service)
        if intent is not None:
            service.outcomes.note(intent, request_id=rid)  # before the request file: no orphan if the answer fails
        record = {
            "v": 1,
            "id": rid,
            "workspace_id": service.workspace_id,
            "principal": call.principal,
            "person": person,
            "op": name,
            "state": "bound" if signer is not None else "prepared",
            "signer": signer,
            "created": now,
            "expires": now + (GRANT_EXPIRE_SECONDS if name == "grant" else EXPIRE_SECONDS),
            "prepared": prepared,
            "trees_checked": now if prepared.get("checks") else None,  # human.get's re-check rate bound (N3)
            # H11d (D-P2-32): the working copy (registration generation and pin) of each linked repo; binding and
            # precommit refuse another one (observe.unavailable), never falling back to the declared copy
            "copies": capture.copies,
            "extras": extras,
            # H12d-2: the readers the request admits, with their verified declarations (not signed, not material)
            "readers": capture.readers,
            "hub_handoff": capture.hub_handoff,  # FB15: prepared to be answered by the hub's addon.requested
            "staged": capture.staged,  # V3 G3: the stage an addon.grant installs from (None: plain grant)
            "sequence": meta,
            "result": template,
            "submit": None,
            "outcome": None,
        }
        # the record holds the inner op's file bytes from now on: they are charged to the preparing UID until the
        # request goes terminal and they are stripped, and the blobs they came from are consumed (§0.10, F9)
        blobs = _blob_ids(specs)
        service.blobs.hold_request(rid, call.principal, call.peer.uid, file_bytes(record), consumed=blobs)
        try:
            requests.write(record)
        except BaseException:
            service.blobs.release_request(rid)
            raise
        service.blobs.consume(call.principal, blobs)
        view = self._view(record)
        if intent is not None:
            service.outcomes.commit(intent, view)
        return view

    @staticmethod
    def _check_pasted_image(inner: dict[str, Any], files: dict[str, bytes]) -> None:
        """R3 part B: a file added inside a sequence is a pasted screenshot (the dashboard's paste path, never the CLI).
        The host reads headers only (:mod:`orch.host.imagecheck`); it never decodes. The signer checks the same."""
        from . import imagecheck

        data = files.get(str(inner.get("path")))
        if inner.get("kind") != "screenshot" or inner.get("ac") or inner.get("task") or data is None:
            raise HostError("invalid.input", "a file in a sequence is a screenshot without a criterion or task")
        try:
            imagecheck.check(data)
        except imagecheck.ImageRefused as exc:
            raise HostError("invalid.input", f"screenshot refused: {exc}") from None

    @staticmethod
    def _recheck_needs(store: Any, record: dict[str, Any], events: list[dict[str, Any]]) -> None:
        """AE4 again under the submit lock, on the re-read state: two requests prepared apart (a grant while the
        setting was unset, a clearing while the add-on was off) must not land as "on, not set up"."""
        from orch.addons import settings_needs

        op = record["op"]
        ws = store.state.workspace
        if op == "addon.grant":
            if (why := settings_needs.grant_refusal(ws.settings, str(events[0].get("name")))) is not None:
                raise HostError("invalid.input", why)
        elif op == "settings.set":
            if (why := settings_needs.clear_refusal(events[0].get("set") or {}, ws.addons)) is not None:
                raise HostError("invalid.input", why)
            if ADDONS_KEY in (events[0].get("set") or {}):  # G6: the granted schema, again on the re-read state
                from orch.addons import settings_value

                if (why := settings_value.check_granted(store, events[0]["set"][ADDONS_KEY])) is not None:
                    raise HostError("invalid.input", f"add-on settings: {why}")

    @staticmethod
    def _sequence(
        call: Call, name: str, inner: dict[str, Any], args: dict[str, Any], person: str, store: Any, dry_run: bool
    ) -> dict[str, Any] | None:
        """``human.prepare {sequence}`` (batch 11, finding 9): one step of a signer's sequence. The meta is checked
        whole (:func:`orch.host.sequence.check_meta`); a later step is prepared only on the head the signer's own
        previous step left (a foreign event in between is ``human.request_mismatch``, the signer's abort)."""
        meta = args.get("sequence")
        if meta is None:
            return None
        from . import sequence

        if args.get("handoff") and meta.get("step") != 0:
            raise HostError("invalid.input", "only the first step of a sequence is a hand-off")
        who = args.get("signer")
        if who is not None and who.get("auth") not in ("passphrase", "convenience"):
            # the phone (and a secure enclave) keep separate signatures: no sequence is offered to them
            raise HostError("invalid.input", "a sequence is signed with the passphrase or one-click custody only")
        try:
            checked = sequence.check_meta(
                meta, op=name, args=inner, workspace_id=call.service.workspace_id, person=person
            )
        except sequence.SequenceInvalid as exc:
            raise HostError("invalid.input", f"sequence: {exc}") from None
        if checked["step"] == 0 and checked["intent"]["kind"] == sequence.QUICK:
            from orch.ops import addon_gate
            from orch.ops import quick as quick_core

            if not addon_gate.is_on(store.state.workspace.addons, quick_core.ADDON):
                raise HostError("addon.off", addon_gate.off_message(quick_core.ADDON))
        if checked["step"] == 0 and checked["intent"]["kind"] == sequence.ADDON:
            from . import seqaddon

            seqaddon.host_check_first(
                store, person, checked["intent"], dry_run=dry_run, err=lambda code, why: HostError(code, why)
            )
        if checked["step"] > 0 and checked.get("log") == "workspace":
            # the add-on sequence's grant: the next step on the workspace log, only on the tail its settings left (AE6)
            if not dry_run and _tail_id(store, "workspace") != checked["prev"]:
                raise HostError("human.request_mismatch", sequence.CHANGED_WS)
            return checked
        if checked["step"] == 0 and checked["intent"]["kind"] == sequence.EDIT and checked["intent"].get("people"):
            from orch.model import claims

            core = store.state._core
            view = store.ticket(checked["intent"]["ticket"]["key"])
            if view is None or not claims.may_manage(core.ws, core.tickets[view.uid], person):
                raise HostError("role.denied", "people are for the ticket owner, owners and maintainers")
        if checked["step"] == 0 and checked["intent"]["kind"] == sequence.EDIT and not dry_run:
            # an edit starts on the ticket and at the head the person reviewed (also checked by the signer)
            want = checked["intent"]["ticket"]
            view = store.ticket(want["key"])
            seq = store.log_seq(want["uid"]) if view is not None and view.uid == want["uid"] else None
            if seq is None or store.head_at(want["uid"], seq) != checked["intent"]["head"]:
                raise HostError("human.request_mismatch", sequence.CHANGED)
        agent = (checked.get("intent") or {}).get("agent") if checked["step"] == 0 else None
        if (
            agent is not None
        ):  # a runner grant lasts at most the workspace's grant_hours (even for an owner): refuse at the review
            from orch.model.types import GRANT_MAX_HOURS

            cap = min(store.state.workspace.settings["grant_hours"], GRANT_MAX_HOURS)
            if agent["hours"] > cap:
                raise HostError(
                    "invalid.input",
                    f"agent access can last at most {cap} hours in this workspace (Settings), not {agent['hours']}; "
                    "nothing was signed",
                )
        if checked["step"] > 0 and not dry_run:
            view = store.ticket(checked["ticket"])
            if (
                inner.get("ref") not in (None, checked["ticket"])
                or view is None
                or _tail_id(store, view.uid) != checked["prev"]
            ):
                raise HostError("human.request_mismatch", sequence.CHANGED)
        return checked

    def _specs(self, operation: Any, frame: dict[str, Any], inner: dict[str, Any]) -> dict[str, Any]:
        """The inner op's ``files``: inline ``{b64}``, a ``{blob}``, or (only on a retry of a prepare with
        ``opts.idem`` that has a record, since the prepare consumed the blob) a ``{digest}``, which only binds."""
        return self.backend._file_specs(operation, frame, inner)

    def _build(
        self, call: Call, name: str, log: str, capture: Any, store: Any, now: float
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """The envelopes, the material, its review, ``review_digest`` and the fingerprint of one request."""
        from orch.identity import new_ulid

        based_on = store.log_head(log)
        roster_v = store.state.workspace.roster_v
        events, extras = [], []
        for c in capture.events:
            ev = {
                "v": 2,
                "id": new_ulid(),  # CSPRNG: 80 random bits (H0 amends F1 §5.1)
                "type": c.event["type"],
                "actor": {"kind": "person", "id": capture.person, "device": capture.device},
                "auth": capture.auth,
                "hash_v": 1,
                "roster_v": roster_v,
                "based_on": based_on,
                **{k: copy.deepcopy(v) for k, v in c.event.items() if k != "type"},
            }
            events.append(ev)
            extras.append(
                {
                    "body": dict(c.body) if c.body else None,
                    "artifacts": {n: base64.b64encode(b).decode() for n, b in (c.artifacts or {}).items()} or None,
                }
            )
        material = self._material(store, log, events, capture, extras)
        try:
            material["review"] = [review.of(e, material) for e in events]
        except review.ReviewError as exc:
            raise HostError("internal", f"the review of {name} cannot be built: {exc}") from None
        from orch.ops.readers import added_readers

        admitted = {r["device"]: r["level"] for r in capture.readers}
        for ev, block in zip(events, material["review"], strict=True):
            if ev["type"] in review.READER_TYPES:  # H12d-2: the reader line (§0.8.6 item 9.11), bound by the digest
                block["derived"]["reader"] = max((admitted.get(d, 0) for d in added_readers([ev])), default=0)
        digest = review.review_digest(material)
        prepared = {
            "op": name,
            "workspace_id": call.service.workspace_id,
            "log": log,
            "events": events,
            "material": material,
            "review_digest": digest,
            "fingerprint": review.fingerprint(digest, events, name),
            "origin": {
                "uid": call.peer.uid,
                "pid": int(call.session.credentials.pid),
                "client": str(((call.session.hello or {}).get("client")) or "unknown")[:64] or "unknown",
                "created_at": _iso(now),
            },
        }
        return prepared, extras

    @staticmethod
    def _material(store: Any, log: str, events: list[dict[str, Any]], capture: Any, extras: list) -> dict[str, Any]:
        from orch.store.render import thaw

        view = store.ticket(log) if log != "workspace" else None
        ticket = {"key": view.key, "uid": view.uid} if view is not None else None
        if view is None and log != "workspace":  # a person's new: the ticket its ticket.created makes
            ticket = {"key": events[0]["key"], "uid": log}
        types = [e["type"] for e in events]
        if len(events) == 1 and types[0] in _GATE_TYPES:
            from orch import canon as _canon

            gate = events[0].get("gate", "verify")
            g = view.gates[gate]
            inp = thaw(g.input)
            texts = store.body_sections(view.uid)  # every section the gate binds, as the signer must show it (P1)
            material = {
                "kind": "gate",
                "gate": gate,
                "input": inp,
                "policy": _canon.canonical_policy(thaw(g.policy)),
                "texts": {sid: texts.get(sid, "") for sid in sorted(inp.get("sections") or {})},
                "ticket": ticket,
            }
            if gate == "verify":
                from orch.model.accept import criterion_hash, parse_label

                t = store.state._core.tickets[view.uid]
                criteria = {a["id"]: a["text"] for a in t.fields["acceptance"]}
                material["manual_acceptances"] = [
                    {"ac": a.ac, "name": a.name, "by": a.by}
                    for a in sorted(t.artifacts.values(), key=lambda a: a.name)
                    if a.kind == "feedback"
                    and a.evidence_ok
                    and a.by.startswith("p_")
                    and a.ac in criteria
                    and parse_label(a.label) == (a.ac, criterion_hash(a.ac, criteria[a.ac]))
                ]
            return material
        if len(events) == 1 and types[0] == "question.answered":
            qid = events[0]["question"]
            question = next((thaw(q) for q in view.fields["questions"] if q["id"] == qid), None)
            return {"kind": "question", "ticket": ticket, "question": question}
        material: dict[str, Any] = {"kind": "payload", "ticket": ticket}
        if capture.statement is not None:
            material["statement"] = capture.statement
        texts: dict[str, str] = {}
        for ev, extra in zip(events, extras, strict=True):
            sections = ev.get("sections")
            for sid in sections if isinstance(sections, dict) else ():  # addon.granted's are declarations (a list)
                body = extra.get("body") or {}
                texts[sid] = body.get(sid) or ""
        if texts:
            material["texts"] = texts
        before = dict(capture.before)
        for ev in events:
            if ev["type"] == "settings.changed":
                current = store.state.workspace
                for key, value in (ev.get("set") or {}).items():  # the host's current values, unsigned (§0.8.1)
                    if key == "repos":
                        now = {name: thaw(current.repos.get(name)) for name in value}
                    elif key == ADDONS_KEY and isinstance(value, dict):  # G6: the host's value of each named add-on
                        held = current.settings.get(ADDONS_KEY) or {}
                        now = {name: thaw(held.get(name)) for name in value}
                    else:
                        now = thaw(current.settings.get(key))
                    before.setdefault(key, now)
        if before:
            material["before"] = before
        taken = [ev["grant"] for ev in events if ev["type"] == "grant.revoked"]
        if taken:  # which access is taken back, as the host's log knows it (unsigned, labelled): the plain words
            known = store.state.workspace.grants
            material["grants"] = {
                gid: {"label": g.label or "", "scope": g.scope, "expires_at": g.expires_at, "person": g.person}
                for gid in taken
                if (g := known.get(gid)) is not None and isinstance(g.scope, str) and isinstance(g.expires_at, str)
            }
        if capture.removed_device is not None and "device.removed" in types:
            material["removed_device"] = dict(capture.removed_device)  # #1198 C15: whose device, per host, not signed
        if capture.market is not None and "addon.granted" in types:
            material["market"] = copy.deepcopy(capture.market)  # V3 G3: the host's word on the source, not signed
        if capture.deps is not None and "addon.granted" in types:
            material["deps"] = copy.deepcopy(capture.deps)  # V3 G4: the package list, from the checked lockfile
        seq = getattr(capture, "sequence", None)
        if seq is not None and seq["step"] == 0:  # the whole intent is in the material: bound by review_digest
            material["sequence"] = {"digest": seq["digest"], "of": seq["of"], "intent": copy.deepcopy(seq["intent"])}
        return material

    # -- reading and binding
    @staticmethod
    def _view(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "request": {
                "id": record["id"],
                "expires_at": _iso(record["expires"]),
                "state": record["state"],
                "submitted_at": _submitted_at(record),
            },
            **record["prepared"],
        }

    def get(self, call: Call, requests: RequestStore, args: dict[str, Any]) -> dict[str, Any]:
        record = requests.load(args["request_id"], call.principal)
        signer = args.get("signer")
        if signer is None:
            return self._view(record)
        state = record["state"]
        if state == "expired":
            raise HostError("human.request_expired", _expired(record))
        if state in ("submitted", "cancelled"):
            raise HostError("human.request_state", f"the request is {state}")
        if state != "bound" and record.get("hub_handoff") is not True:
            # the only request stored unbound is the hub's add-on grant hand-off (FB15); every other request is bound
            # at prepare, so an unbound one that is not a hand-off was not prepared here: never bound (no legacy path)
            raise HostError("invalid.input", "the request was not prepared here for a hub hand-off; prepare it again")
        call.service.store.refresh()
        convenience.refuse_reader_op(  # §0.8.6: fails early, before binding (H12d-2: the declaration, rechecked)
            call.service,
            record["op"],
            history=True,
            events=record["prepared"]["events"],
            readers=record.get("readers") or [],
        )
        if record["op"] in hubproof.OPS:  # #1198 C9: a proof sent with the bind is checked now; One-click needs one
            sent = args.get("owner_proof")
            if sent is not None or signer["auth"] == "convenience":
                hubproof.check(
                    call.service, person=record["person"], op=record["op"], request_id=record["id"],
                    event=record["prepared"]["events"][0], proof=sent,
                )  # fmt: skip
        if record.get("sequence") is not None and signer["auth"] not in ("passphrase", "convenience"):
            # a sequence is bound to the passphrase or one-click custody only, here as at prepare (not the phone)
            raise HostError("invalid.input", "a sequence is signed with the passphrase or one-click custody only")
        unsigned = record["prepared"]["events"]
        if signer["auth"] == "convenience":  # §0.8.6: before a convenience binding, and on every bound read
            convenience.check(
                call.service,
                record["person"],
                signer["device"],
                removing=_removing(record["op"], [e.get("device") for e in unsigned]),
                leaving=record["op"] == "hub.leave",
            )
        if state == "bound":
            if record["signer"] != signer:
                raise HostError("human.request_state", "the request is bound to another device")
            # H11d (Codex #439): the copy binding and the ticket's current branch on every signer-bearing get,
            # independent of the tree-check throttle below
            check_bindings(call, record)
            fresh = call.service.clock() - float(record.get("trees_checked") or 0) < RECHECK_SECONDS
            if _binds_commits(record["prepared"]) and not fresh:  # N3: tree.check re-run at binding gets
                record = recheck_trees(call, record)
                requests.write(record)
            return self._view(record)
        store = call.service.store
        store.refresh()
        events = record["prepared"]["events"]
        if record["op"] != ROTATE_OP:
            check_device(
                store,
                record["person"],
                signer["device"],
                call.service.clock(),
                operate=_operate([e["type"] for e in events]),
            )
        if _binds_commits(record["prepared"]):  # N3: a tree dirtied since prepare is refused here, nothing bound
            check_bindings(call, record)
            record = recheck_trees(call, record)
        for ev in unsigned:  # the one change binding makes: null -> the signer's device and auth
            ev["actor"]["device"] = signer["device"]
            ev["auth"] = signer["auth"]
        record = {**record, "state": "bound", "signer": signer}
        requests.write(record)
        return self._view(record)

    def pending(self, call: Call, requests: RequestStore, args: dict[str, Any]) -> dict[str, Any]:
        """This principal's requests, optionally selected by id; submit errors carry catalog codes only."""
        if "request_id" in args:
            try:
                records = [requests.load(args["request_id"], call.principal)]
            except HostError:
                records = []  # an unknown request gives an empty list, like an unknown code
        else:
            # one file read per request in this workspace's directory: bounded by the 20-pending quota per person
            # plus the terminal requests kept 24 h; an index can replace it if H5b's hand-offs make lists long
            records = []
            for rid in requests.ids():
                try:
                    rec = requests.read(rid)
                except HostError:
                    continue
                if rec is not None and rec.get("principal") == call.principal:
                    records.append(requests.expire(rec))
        return {"requests": [self._summary(r) for r in records]}

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        prepared = record["prepared"]
        ticket = prepared["material"].get("ticket")
        outcome = record.get("outcome") or {}
        out: dict[str, Any] = {
            "id": record["id"],
            "op": record["op"],
            "key": ticket["key"] if ticket else None,
            "created_at": _iso(record["created"]),
            "expires_at": _iso(record["expires"]),
            "submitted_at": _submitted_at(record),
            "state": record["state"],
            "event_ids": [e["id"] for e in prepared["events"]],
            "seq": (outcome.get("result") or {}).get("seq") if outcome.get("state") == "committed" else None,
        }
        error = (outcome.get("error") or {}).get("code") or (record.get("error") or {}).get("code")
        if error:
            out["error"] = {"code": error}
        return out

    def cancel(self, call: Call, requests: RequestStore, args: dict[str, Any]) -> dict[str, Any]:
        record = requests.load(args["request_id"], call.principal)
        if record.get("submit"):  # its signed events are being appended: it ends submitted, never cancelled
            return {"state": "submitted"}
        if record["state"] in PENDING:
            record = {**record, "state": "cancelled", "done_at": call.service.clock()}
            requests.write(record)
        # an expired request can never be submitted either: it answers cancelled (§0.8.1 lists two answers)
        return {"state": "submitted" if record["state"] == "submitted" else "cancelled"}

    # -- submit
    def submit(self, call: Call, args: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
        service = call.service
        opts = call.frame.get("opts") or {}
        if call.frame.get("files"):
            raise HostError("blob.invalid", "human.submit takes no files")
        idem = request_idem("human.submit", opts)  # dry_run is invalid.input; no idem is idem.required
        assert idem is not None
        outcomes = service.outcomes
        # the outcome record (written before verification, kept after a refusal) holds a digest of each signature,
        # never the raw value: only request_id is read back from it (outcome_fields); the binding is over the digests
        begun = outcomes.begin(
            call.principal, idem, "human.submit", _sigs_digested(args), planned=(), deferred=True, session=None
        )
        if isinstance(begun, dict):
            return begun  # §0.6 step 9: the stored answer of this idem
        try:
            self.backend._state(service, True)
        except BaseException:
            outcomes.abandon(begun)
            raise
        try:
            result = redactor.obj(self._submit(call, args, begun))
        except _Stored as stored:
            # the request's stored failure (or a roll-forward that failed after an event landed): stored as failed
            # under this idem whatever its retryability, so a retry replays it; a plain HostError below is a refusal
            # before anything landed, which refuse() stores only when it is not retryable
            outcomes.fail(begun, stored.error)
            raise stored.error from None
        except HostError as exc:  # a refusal before any append: the request stays as it was (bound), until it expires
            outcomes.refuse(begun, redactor.error(exc))
            self._note_refusal(call, args["request_id"], exc.code)
            raise redactor.error(exc) from None
        except BaseException:
            outcomes.release(begun)  # the log and the request say what landed; start-up recovery finishes it
            raise
        return outcomes.commit(begun, result)["result"]

    def _note_refusal(self, call: Call, request_id: str, code: str) -> None:
        """The last refused submit's catalog code, for ``human.pending`` (never the message)."""
        requests = self._requests(call.service)
        try:
            record = requests.load(request_id, call.principal)
        except HostError:
            return
        if record["state"] == "bound" and not record.get("submit"):
            requests.write({**record, "error": {"code": code}})

    def _submit(self, call: Call, args: dict[str, Any], intent: Any) -> dict[str, Any]:
        service = call.service
        requests = self._requests(service)
        record = requests.load(args["request_id"], call.principal)
        events = args["events"]
        sub = record.get("submit")
        same = bool(sub) and canon.dumps(sub["events"]) == canon.dumps(events)
        if sub and record.get("outcome") is None:
            # a submission interrupted between two appends (not a crash: the host kept serving). The signatures exist,
            # so it is rolled forward exactly as at start, never judged again; other events get request_state
            if not same:
                raise HostError("human.request_state", "the request is being submitted with other events")
            result = roll_forward(service, requests, record)
            return {**result, "duplicate": True}
        if record["state"] == "submitted" and same:
            outcome = record["outcome"]
            if outcome["state"] == "committed":
                return {**outcome["result"], "duplicate": True}
            raise _Stored(HostError.stored(outcome["error"]))
        if record["state"] != "bound":
            if record["state"] == "expired":
                raise HostError("human.request_expired", _expired(record))
            raise HostError("human.request_state", f"the request is {record['state']}, not bound to a signer")
        prepared = record["prepared"]["events"]
        if len(events) != len(prepared):
            raise HostError("human.request_mismatch", "the submitted events differ from the prepared ones")
        for got, want in zip(events, prepared, strict=True):
            if not isinstance(got.get("sig"), str):
                raise HostError("sig.invalid", "every submitted event carries its sig")
            if canon.dumps(_unsigned(got, grant=True)) != canon.dumps(_unsigned(want, grant=True)):
                raise HostError("human.request_mismatch", "the submitted events differ from the prepared ones")
            if want["type"] == "grant.issued" and not self._fresh_secret_hash(service.store, got.get("secret_hash")):
                raise HostError("human.request_mismatch", "the grant's secret_hash is not a new, well-formed hash")
        store = service.store
        with store.locked():
            record = requests.expire(record)  # expiry is checked again at append, under the lock
            if record["state"] == "expired":
                raise HostError("human.request_expired", _expired(record, "before it was appended"))
            store.refresh()  # unconditionally: the history and roster checks below read what is on disk now
            from . import sequence

            meta = record.get("sequence")
            if meta is not None and meta["step"] == 0 and meta["intent"]["kind"] == sequence.EDIT:
                # B12D: an edit's first step lands only on the head the person reviewed, re-checked here under the lock
                if store.log_head(record["prepared"]["log"]) != events[0].get("based_on"):
                    raise HostError("human.request_mismatch", sequence.CHANGED)
            if meta is not None and meta["step"] > 0 and meta.get("log") == "workspace":
                # the add-on sequence's grant (AE6): on the workspace tail the signer's own settings step left, re-read
                # here under the lock; it is the grant of an add-on, on the workspace log, and nothing else
                if (
                    record["op"] != "addon.grant"
                    or record["prepared"]["log"] != "workspace"
                    or _tail_id(store, "workspace") != meta["prev"]
                ):
                    raise HostError("human.request_mismatch", "the workspace changed while signing, review again")
            elif meta is not None and meta["step"] > 0:
                # batch 11: a later step of a signer's sequence lands only on the head its previous step left, checked
                # here under the lock on the re-read log (a foreign event since the prepare is the signer's abort)
                log = record["prepared"]["log"]
                view = store.ticket(meta["ticket"])
                if view is None or _tail_id(store, view.uid) != meta["prev"]:
                    raise HostError("human.request_mismatch", "the ticket changed while signing, review again")
                if log != "workspace" and store.log_head(log) != events[0].get("based_on"):
                    raise HostError("human.request_mismatch", "the ticket changed while signing, review again")
                if log not in ("workspace", view.uid) or (log == "workspace" and record["op"] != "grant"):
                    raise HostError("human.request_mismatch", "the ticket changed while signing, review again")
            self._recheck_needs(store, record, events)
            marketinstall.recheck(
                service, record, events
            )  # V3 G3 C2: the install record as it is now, before the journal
            if record["op"] in hubproof.OPS:  # #1198 C9: every hub event carries a valid proof, checked under the lock
                for e in events:
                    hubproof.check(
                        service, person=record["person"], op=record["op"], request_id=record["id"],
                        event=e, proof=e.get("owner_proof"),
                    )  # fmt: skip
            if any(e.get("auth") == "convenience" for e in events):  # §0.8.6: the condition, again at submit
                convenience.check(
                    service,
                    record["person"],
                    events[0]["actor"]["device"],
                    removing=_removing(record["op"], [e.get("device") for e in events]),
                    leaving=record["op"] == "hub.leave",
                )
            # §0.8.6: a reader is never added to a history with convenience signatures, nor while the flag is on,
            # however long ago the request was prepared
            convenience.refuse_reader_op(
                service,
                record["op"],
                history=True,
                events=record["prepared"]["events"],
                readers=record.get("readers") or [],
            )
            git = service.caps_for(call.principal).git
            self._judge(store, record, events, git)
            log = record["prepared"]["log"]
            view = store.ticket(log) if log != "workspace" else None
            key = view.key if view is not None else None
            outcomes = service.outcomes
            outcomes.plan(intent, [Planned(log, e["id"], e["type"], e["actor"]) for e in events], key)
            record = {
                **record,
                "submit": {"events": events, "outcome_key": intent.key},
                "submitted_at": requests.clock(),
            }
            requests.write(record)  # fsync before the first append: a crash after it is rolled forward at start
            return finish(service, requests, record)

    @staticmethod
    def _fresh_secret_hash(store: Any, value: Any) -> bool:
        import re

        if not isinstance(value, str) or not re.fullmatch(_HASH, value):
            return False
        return all(store.grant_secret_hash(g) != value for g in store.state.workspace.grants)

    @staticmethod
    def _judge(store: Any, record: dict[str, Any], events: list[dict[str, Any]], git: Any) -> None:
        """:func:`judge_events` over the prepared log, with the request's bound working copies (H11d)."""
        judge_events(store, record["prepared"]["log"], events, git=git, copies=record.get("copies"))


def judge_events(
    store: Any,
    log: str,
    events: list[dict[str, Any]],
    *,
    git: Any = None,
    copies: list[dict[str, Any]] | None = None,
) -> None:
    """**The model's judgement of submitted, device-signed events**, shared by ``human.submit`` and ``relay.submit``
    (§11A.14 step L6, H12e). The caller holds ``store.locked()`` (after ``store.refresh()``); nothing is appended.

    All events together with ``model.preview`` on the stamp the store would give them now (``peek_stamp``): roster,
    ``based_on``, the question hash, authority, scopes and the person signature are checked at that position (the
    prepare-time judging in :func:`orch.ops.prepare.judge` set signatures aside). Every ``source_sha`` list is also
    rechecked against git under the same lock (D58, :func:`recheck_sources`): that needs ``git`` (the caller's
    principal's reader) and fails closed without it (``internal``); the relay's two types carry none.

    Raises :class:`HostError` with the model's code (``question.stale``, ``members.stale``, ``event.bad_base``,
    ``sig.invalid``, ``device.scope``, ``ticket.exists``, ...) at the first refused event; returns ``None``. An
    unexpected exception of the model on malformed but signed input (``KeyError``, ``TypeError``, ``ValueError``...)
    never escapes: it is ``validation.event`` (H12e-2)."""
    from orch.model import Refusal, preview
    from orch.store import StoreError

    state = store.state
    stamp = store.peek_stamp(log)
    seq, prev, at = stamp["seq"], stamp["prev"], stamp["at"]
    for ev in events:
        if "source_sha" in ev and log != "workspace":  # D58 first: git is read under the same lock
            if git is None:
                raise HostError("internal", "a signed source list needs the git reader; nothing was judged")
            recheck_sources(git, store, store.ticket(log), ev["source_sha"], copies)
        probe = {**ev, "seq": seq, "prev": prev, "at": at}
        if "ws_seq" in stamp:
            probe["ws_seq"] = stamp["ws_seq"]
        try:
            r = preview(state, probe, log=log)
        except Exception as exc:  # noqa: BLE001 - signed but malformed input must end as a definite refusal (H12e-2)
            _log.warning("judge_events: the model raised %s; refused as validation.event", type(exc).__name__)
            raise HostError("validation.event", f"the event cannot be judged ({type(exc).__name__})") from exc
        if isinstance(r, Refusal):
            raise _store_error(StoreError.from_refusal(r))
        state, seq, prev = r, seq + 1, canon.event_head(probe)


#: a bound request's later ``human.get`` reuses a binding check younger than this instead of walking the tree again
#: (a client polling the request must not keep the workspace's worker thread busy); binding itself always checks
RECHECK_SECONDS = 5.0


def recheck_trees(call: Call, record: dict[str, Any]) -> dict[str, Any]:
    """N3: run ``tree.check`` again, now, for every working copy a prepared request's ``checks`` name, when
    ``human.get`` binds a device and at later binding gets. Each answer must be clean **at the signed commit**: the
    child's ``head`` and ``head_ref`` (read in the same child as the tree) must equal the ``source_sha`` entry of that
    repo's identity. Then every repo the request binds must be covered (:func:`require_checks`). A changed tree, another
    commit or branch, or no answer (unknown) is ``observe.unavailable`` and nothing is bound; otherwise the record
    comes back with fresh ``checks`` (only their ``at`` changes; they are not signed). The stored checks are never
    trusted."""
    from orch.gitread.treejob import check_entry

    prepared = record["prepared"]
    signed = {e["repo"]: e for ev in prepared["events"] for e in ev.get("source_sha") or []}
    service = call.service
    git = service.caps_for(call.principal).git
    store = service.store
    fresh = []
    covered: set[str] = set()
    for c in prepared.get("checks") or []:
        name = c["repo"]
        path = bound_copy(git, store, prepared["log"], name, record.get("copies"))
        got = git.tree_check(path, name) if path is not None else None
        if got is None:
            raise HostError("observe.unavailable", f"{name}: the working tree cannot be checked now; nothing was bound")
        identity = git.identity(path, name)
        entry = signed.get(identity)
        if entry is None or got["head"] != entry["sha"] or got["head_ref"] != entry["ref"]:
            raise HostError(
                "observe.unavailable",
                f"{name}: the working tree is not at the commit the request binds any more; nothing was bound",
            )
        if got["changed"] or got["more"]:
            first = got["changed"][0] if got["changed"] else {"why": "modified", "path": "…"}
            raise HostError(
                "observe.unavailable",
                # the path is escaped already (F1 section 11.3, treejob.escape_path)
                f"{name}: the working tree changed since prepare ({first['why']}: {first['path']})"[:300],
            )
        covered.add(identity)
        fresh.append(check_entry(name, service.clock()))
    missing = sorted(set(signed) - covered)
    if missing:
        raise HostError(
            "observe.unavailable", f"{missing[0]}: the working tree of a bound repository was not checked"[:300]
        )
    return {**record, "trees_checked": service.clock(), "prepared": {**prepared, "checks": fresh}}


def check_bindings(call: Call, record: dict[str, Any]) -> None:
    """H11d (D-P2-32, Codex #439): on every signer-bearing ``human.get`` of a request that binds commits, whether or
    not the tree check is due: each linked repo with a branch must still resolve to the working copy the request was
    prepared against (:func:`bound_copy`: generation, registration, pin, the ``.git`` file), and the ticket's
    **current** ``links.branches`` (loaded fresh) must still be the branch the request binds for that repo. A relink
    after prepare, a removal or a recreation is ``observe.unavailable``; nothing is bound or returned."""
    prepared = record["prepared"]
    if not _binds_commits(prepared):
        return
    service = call.service
    git = service.caps_for(call.principal).git
    store = service.store
    view = store.ticket(prepared["log"])
    if view is None:
        raise HostError("observe.unavailable", "the ticket cannot be read now; nothing was bound")
    links = view.fields["links"]
    signed = {e["repo"]: e for ev in prepared["events"] for e in ev.get("source_sha") or []}
    names = {c["repo"] for c in prepared.get("checks") or []} | {n for n in links["repos"] if links["branches"].get(n)}
    for name in sorted(names):
        path = bound_copy(git, store, prepared["log"], name, record.get("copies"))
        branch = links["branches"].get(name) if name in links["repos"] else None
        entry = signed.get(git.identity(path, name))
        if not branch or entry is None or entry.get("ref") != f"refs/heads/{branch}":
            raise HostError(
                "observe.unavailable",
                f"{name}: the ticket's branch changed since the request was prepared; prepare it again",
            )


def _binds_commits(prepared: dict[str, Any]) -> bool:
    return any(ev.get("source_sha") for ev in prepared["events"])


def bound_copy(git: Any, store: Any, log: str, name: str, copies: list[dict[str, Any]] | None) -> Any:
    """H11d (D-P2-32, §12.7): the path of the working copy of (ticket ``log``, repo ``name``) **if** it is still the
    one the request was prepared against: the same registration generation, the same registered (or declared) copy
    and the same worktree pin. A re-registration, a removal or a re-pin since prepare is ``observe.unavailable``;
    there is never a fallback to the declared working copy."""
    if not git.enabled or git.reader is None:  # fail closed: no reader means nothing can vouch for any copy
        raise HostError("observe.unavailable", f"{name}: git cannot be read in this workspace now")
    want = next((c for c in copies or [] if c.get("repo") == name and c.get("ticket") == log), None)
    copy = git.ticket_copy(store.layout, store.state.workspace.repos, log, name)
    if want is None or copy.binding(log, name) != want:
        raise HostError(
            "observe.unavailable",
            f"{name}: the ticket's worktree registration changed since the request was prepared; prepare it again",
        )
    if copy.path is None:
        raise HostError("observe.unavailable", (copy.why or f"{name}: no working copy")[:300])
    if copy.registered:  # the .git file and the directory are what was pinned (a re-pointed one never falls back)
        from orch.gitread.errors import Unavailable

        try:
            git.reader.verify_worktree(os.fspath(copy.path), principal=git.principal)
        except Unavailable as exc:
            raise HostError("observe.unavailable", f"{name}: {exc.reason}"[:300]) from None
    return copy.path


def require_checks(call: Call, prepared: dict[str, Any], copies: list[dict[str, Any]] | None = None) -> None:
    """At prepare: every repository a prepared event binds (each ``source_sha`` entry, by identity) must have a
    clean-tree check in ``checks``; otherwise ``observe.unavailable``. The checked repos' identities are read again
    through the reader (in the working copy the check ran in, H11d), so a check of one repo can never stand in for
    another."""
    need = {e["repo"] for ev in prepared["events"] for e in ev.get("source_sha") or []}
    if not need:
        return
    service = call.service
    git = service.caps_for(call.principal).git
    store = service.store
    have: set[str] = set()
    for c in prepared.get("checks") or []:
        path = bound_copy(git, store, prepared["log"], c["repo"], copies)
        if path is not None and c.get("clean") is True:
            have.add(git.identity(path, c["repo"]))
    missing = sorted(need - have)
    if missing:
        raise HostError(
            "observe.unavailable", f"{missing[0]}: the working tree of a bound repository was not checked"[:300]
        )


def recheck_sources(
    git: Any, store: Any, view: Any, signed: list[dict[str, Any]], copies: list[dict[str, Any]] | None = None
) -> None:
    """D58 at submit, under the store lock: each linked repo's head, read again through the host's git reader (under
    the caller's principal), must be the commit the decision signs, on the bound branch: ``gate.stale`` otherwise. A
    reader that cannot answer fails closed (``observe.unavailable``). The working tree's cleanliness is ``tree.check``
    at prepare and binding (:func:`recheck_trees`, N3), not checked here (H0 amends F1 section 10.8).

    H11d (D-P2-32): the head is read in the working copy the request was prepared against (:func:`bound_copy`: the
    same registration generation and pin, never a fallback to the declared copy), and ``HEAD`` must still be the
    symbolic ref of the ticket's **current** branch."""
    from orch.ops.caps import CapabilityDenied

    links = view.fields["links"]
    for name in links["repos"]:
        branch = links["branches"].get(name)
        if not branch:
            continue
        try:
            path = bound_copy(git, store, view.uid, name, copies)
            head = git.head(path) if path is not None else None
            on = git.symbolic_head(path) if head is not None else None
            identity = git.identity(path, name) if head is not None else None
        except (CapabilityDenied, OSError) as exc:
            raise HostError("observe.unavailable", f"{name}: git cannot be read now ({exc})") from None
        if head is None or identity is None:
            raise HostError("observe.unavailable", f"{name}: git cannot be read now")
        entry = next((e for e in signed if e.get("repo") == identity), None)
        if entry is None or entry.get("ref") != f"refs/heads/{branch}" or entry.get("sha") != head:
            raise HostError("gate.stale", f"{name}: the code moved since the request was prepared; look again")
        if on != f"refs/heads/{branch}":
            raise HostError("observe.unavailable", f"{name}: the working tree is not on the ticket's branch {branch}")


class _Stored(Exception):
    def __init__(self, error: HostError) -> None:
        self.error = error


def _mark_seen(service: WorkspaceService) -> HostError | None:
    """Set the durable ``convenience_seen`` mark (:mod:`orch.ops.readers`); the error when it cannot be written, which
    fails the submission before anything is appended (fail closed)."""
    from pathlib import Path

    from orch.ops.readers import ensure_convenience_mark

    try:
        ensure_convenience_mark(Path(service.state_dir), service.workspace_id, service.host_uid, clock=service.clock)
    except Exception as exc:  # noqa: BLE001 - any doubt refuses the append
        return HostError(
            "custody.unsafe_mode",
            f"the convenience_seen mark could not be written ({type(exc).__name__}); nothing was appended",
        )
    return None


def _readers_refusal(
    service: WorkspaceService, log: str, events: list[dict[str, Any]], readers: list[dict[str, Any]]
) -> HostError | None:
    """Invariant I at the append (§0.8.6 item 9.4), under the store lock: the refusal, or ``None`` after the admitted
    readers' records were written. Only the events not in the log yet count (a roll forward of a landed event)."""
    from pathlib import Path

    from orch.ops import readers as rd
    from orch.ops.human import _Declared
    from orch.store.readerrecs import relay_level

    store = service.store
    new = [e for e in events if store.logged_event(log, e.get("id")) is None]
    kinds = (*rd.READER_EVENTS, "restore")
    if not any(e.get("type") in kinds or e.get("auth") == "convenience" for e in new):
        return None
    store.refresh()
    ws = store.state.workspace
    adds = rd.added_readers(new, ws)
    state_dir = Path(service.state_dir)
    lv = rd.levels(state_dir, service.workspace_id, store.state, service.host_uid)
    try:
        lv.update(convenience.recheck_readers(service, new, [r for r in readers if r.get("device") in adds]))
    except HostError as exc:
        return exc
    required = 0
    if adds:
        flag = convenience.flag_or_gate(service)  # §11A.17.4: 1 while the dev gate passes, as while the flag is on
        required = rd.required_reader(store, state_dir, service.workspace_id, flag=flag, uid=service.host_uid)
    try:
        relay = relay_level(state_dir, service.workspace_id, service.host_uid)
        rd.check_readers(ws, lv, new, required=required, relay_level=relay)
    except rd.ReadersRefused as exc:
        return convenience.readers_error(exc)
    stamp = _iso(service.clock())
    try:  # every added device starts clean: only the declaration admitted here may count (#513 review)
        rd.drop_stale(state_dir, service.workspace_id, adds)
    except Exception as exc:  # noqa: BLE001 - a stale record that stays would count again: refuse
        return HostError(
            "custody.unsafe_mode",
            f"a stale reader record could not be removed ({type(exc).__name__}); nothing appended",
        )
    for entry in readers:
        if entry.get("device") not in adds:
            continue
        try:
            rd.store_record(
                state_dir,
                service.workspace_id,
                service.host_uid,
                rd.make_record(_Declared(entry), entry["decl"], entry["person"], entry["source"], stamp),
            )
        except Exception as exc:  # noqa: BLE001 - any doubt refuses the append
            return HostError(
                "custody.unsafe_mode",
                f"the reader record could not be written ({type(exc).__name__}); nothing was appended",
            )
    return None


def _signed_by(
    events: list[dict[str, Any]], person: str, device: str, *, service: Any = None, gate: Any = None
) -> HostError | None:
    """The relay path's precondition of :func:`append_signed`: every event is a person event of exactly this
    (person, device) pair, signed (``sig``), and never ``auth: "convenience"`` (§11A.4, §11A.6), **except** a
    ``question.answered`` that carries the :class:`~orch.host.devmode.DevGate` of this job's L2 (``gate``) **and** for
    which a fresh :func:`~orch.host.devmode.dev_gate` call now returns an equal gate (§11A.17.4 L8, F6). Checked
    before anything lands; the store still verifies every signature against the device's certificate when it
    appends."""
    for ev in events:
        actor = ev.get("actor") if isinstance(ev, dict) else None
        if actor != {"kind": "person", "id": person, "device": device}:
            return HostError("op.not_allowed", "column R appends only events signed by the requesting device")
        sig = ev.get("sig")
        if not isinstance(sig, str) or not sig:
            return HostError("op.not_allowed", "column R appends only device-signed events")
        if ev.get("auth") == "convenience" and not _dev_gate_holds(service, gate, ev):
            return HostError("op.not_allowed", "column R appends only device-signed events")
    return None


def _dev_gate_holds(service: Any, gate: Any, ev: dict[str, Any]) -> bool:
    """F6: the token of L2 and a fresh dev gate for the same sandbox and workspace, for an answer only."""
    from .devmode import GATED_TYPE, DevGate, dev_gate

    if service is None or not isinstance(gate, DevGate) or ev.get("type") != GATED_TYPE:
        return False
    fresh = dev_gate(service)
    return fresh is not None and fresh == gate and gate.workspace_id == service.workspace_id


def append_signed(
    service: WorkspaceService,
    log: str,
    events: list[dict[str, Any]],
    extras: list[dict[str, Any]],
    *,
    signed_by: tuple[str, str] | None,
    readers: list[dict[str, Any]] | None = None,
    dev_gate: Any = None,
) -> tuple[list[int], HostError | None]:
    """**The single choke point for appending signed person events** (§0.8.1, §11A.4): ``human.finish`` and the relay
    path (through :func:`append_relay`, the relay's only entry) both append through it. The one documented exception
    is the owner-signed genesis of ``host.bootstrap`` (``control.py``), appended on the control socket before the
    workspace is served; no column reaches it.

    Appends the events of ``events`` that are not in ``log`` yet, in order, under the store lock, with an append check
    that refuses any event the list does not name. Stops at the first refusal. Returns the ``seq`` of every event of
    the list that is in the log (landed now or before) and the refusal, or ``None``. A refusal after the first event
    landed is the caller's ``failed`` outcome, never ``partial``.

    ``signed_by`` is keyword-required: ``None`` on the person path, ``(person, device)`` on the relay path (only
    :func:`append_relay` passes it). There every event must be that pair's signed person event, never
    ``auth: "convenience"``, checked before anything lands (``op.not_allowed``), and an event whose id is already in
    the log counts as landed only when its ``sig`` and ``actor`` are the submitted ones (else ``op.not_allowed``:
    never a false ``committed``).

    The durable ``convenience_seen`` mark (#441) is written here, under the store lock and before the first append,
    when any event carries ``auth: "convenience"``; if it cannot be written nothing is appended (fail closed). On the
    relay path ``signed_by`` refuses convenience events except a dev-gated answer (§11A.17.4, ``dev_gate``), which
    writes the mark here like any other.

    **Hub mode (A1, D83)** does not limit who signs here: a local signature is valid in every hub mode, with the hub
    reachable or not. ``hubdeliver.land`` (custodial events) does not pass here.

    **Invariant I (H12d-2, §0.8.6 item 9.4)** is checked here too, under the store lock and before the first append,
    for the events not in the log yet that add a reader (``member.added``, ``device.added``, ``restore``) or carry
    ``auth: "convenience"`` (:func:`_readers_refusal`): the current levels, the declarations of ``readers`` (the
    request's admitted readers) and ``required_reader`` recomputed. Their records are written before the append
    (``custody.unsafe_mode`` if that fails, nothing appended). Whichever of a convenience event and a reader event
    appends first, the other refuses.
    """
    from orch.store import StoreError

    if signed_by is not None:
        refused = _signed_by(events, *signed_by, service=service, gate=dev_gate)
        if refused is not None:
            return [], refused
    store = service.store
    ids = {(log, e["id"]) for e in events}
    landed: list[int] = []
    error: HostError | None = None
    with store.locked():
        old_check = store.append_check

        def check(lg: str, event: dict[str, Any]) -> None:
            if (lg, event.get("id")) not in ids:
                raise StoreError("internal", f"{event.get('type')}: an append its request does not list")

        store.append_check = check
        try:
            if error is None:
                error = _readers_refusal(service, log, events, readers or [])  # H12d-2: invariant I, before anything
            if error is None and any(e.get("auth") == "convenience" for e in events):
                error = _mark_seen(service)  # #441: the durable mark, under the store lock, before the first append
            for ev, extra in zip(events, extras, strict=True) if error is None else ():
                found = store.logged_event(log, ev["id"])
                if found is not None:
                    if signed_by is not None and (
                        found.get("sig") != ev.get("sig") or found.get("actor") != ev.get("actor")
                    ):
                        error = HostError("op.not_allowed", "an event with this id and another signer is in the log")
                        break
                    landed.append(int(found["seq"]))
                    continue
                artifacts = {n: base64.b64decode(b) for n, b in (extra.get("artifacts") or {}).items()} or None
                try:
                    done = store.append(ev, log=log, body=extra.get("body"), artifacts=artifacts)
                except StoreError as exc:
                    error = _store_error(exc)
                    break
                landed.append(int(done.event["seq"]))
        finally:
            store.append_check = old_check
    if landed and signed_by is None and any(e.get("auth") == "convenience" for e in events):
        convenience.touch(service.state_dir, service.workspace_id, service.host_uid)  # #1198 C17: resets the idle clock
    return landed, error


def append_relay(
    service: WorkspaceService,
    log: str,
    events: list[dict[str, Any]],
    extras: list[dict[str, Any]],
    *,
    person_id: str,
    device_id: str,
    dev_gate: Any = None,
) -> tuple[list[int], HostError | None]:
    """**The relay path's only append** (§11A.4, R2/R5): :func:`append_signed` with the device check always on.
    ``orch/relay`` never calls ``append_signed`` itself (a test enforces it). ``dev_gate``: the
    :class:`~orch.host.devmode.DevGate` of this column-R job's L2 (§11A.17.4), else ``None`` (a roll forward has
    none: a convenience event is then refused, fail closed)."""
    if not isinstance(person_id, str) or not isinstance(device_id, str) or not person_id or not device_id:
        return [], HostError("op.not_allowed", "a relay append needs the requesting device and its person")
    return append_signed(service, log, events, extras, signed_by=(person_id, device_id), dev_gate=dev_gate)


def finish(service: WorkspaceService, requests: RequestStore, record: dict[str, Any]) -> dict[str, Any]:
    """Append the signed events of ``record`` that are not in the log yet, in order, under the store lock
    (:func:`append_signed`), then end the request ``submitted`` with its outcome. Used by ``human.submit`` and by
    start-up recovery (roll forward). A refusal after the first event landed ends the request ``failed`` naming the
    ``seq`` s that landed; it is never ``partial``. Returns the result; raises the stored error for a failed one."""
    store = service.store
    log = record["prepared"]["log"]
    signed = record["submit"]["events"]
    landed, error = append_signed(
        service,
        log,
        signed,
        record["extras"],
        signed_by=None,
        readers=record.get("readers") or [],
    )
    now = service.clock()
    if error is not None:
        seqs = ", ".join(map(str, landed)) or "none"
        failed = HostError(error.code, f"{error.message} (landed: seq {seqs})"[:400], detail=error.detail)
        failed.exit, failed.retryable = error.exit, False
        outcome = {"state": "failed", "error": failed.error(), "exit": failed.exit, "seqs": landed}
        requests.write({**record, "state": "submitted", "done_at": now, "outcome": outcome})
        raise _Stored(failed) if landed else failed
    marketinstall.after_grant(
        service, record, signed
    )  # V3 G3 C3: the swap and the records, idempotent, also at roll-forward
    template = record.get("result") or {}
    view = store.ticket(log) if log != "workspace" else None
    result = ok_result(template.get("data") or {}, seq=max(landed))
    result["key"] = view.key if view is not None else template.get("key")
    result["hints"] = list(template.get("hints") or [])[:8]
    result["lines"] = [str(line) for line in template.get("lines") or []][:10000]  # a record before H7 has none
    outcome = {"state": "committed", "result": result, "exit": 0}
    requests.write({**record, "state": "submitted", "done_at": now, "outcome": outcome})
    return result


def _blob_ids(specs: dict[str, Any]) -> list[str]:
    return [spec["blob"] for spec in specs.values() if "blob" in spec]


def roll_forward(service: WorkspaceService, requests: RequestStore, record: dict[str, Any]) -> dict[str, Any]:
    """Finish an interrupted submission (:func:`finish`) and give the outcome record of the ``human.submit`` that
    started it the same terminal state. Returns the result or raises the stored error."""
    try:
        return finish(service, requests, record)
    finally:
        done = requests.read(record["id"]) or record
        key = (done.get("submit") or {}).get("outcome_key")
        if key and done.get("outcome"):
            service.outcomes.settle(key, done["outcome"])


def first_view(record: dict[str, Any]) -> dict[str, Any]:
    """The answer ``human.prepare`` gave for ``record``: an unbound request is ``prepared``, any other
    one was bound to its signer at prepare."""
    return HumanOps._view({**record, "state": "bound" if record.get("signer") else "prepared"})


def outcome_prepared(service: WorkspaceService, record: dict[str, Any]) -> dict[str, Any] | str:
    """The reconciler hook of the outcome store for a ``pending`` ``human.prepare`` record that names its request: the
    request is stored, so the call committed (with its first view); not stored and older than the request's lifetime
    plus its retention, the idem is a tombstone (never a new request under it); otherwise nothing was created."""
    rid = record.get("request_id")
    if not rid or service.requests is None:
        return "none"
    stored = service.requests.read(rid)
    if stored is not None and stored.get("principal") == record.get("principal"):
        return {"step": "committed", "result": first_view(stored), "exit": 0}
    if service.clock() - float(record.get("at", 0)) > EXPIRE_SECONDS + KEEP_SECONDS:
        return {"step": "tombstone", "state": "failed", "exit": 0}
    return "none"


def outcome_fields(service: WorkspaceService, record: dict[str, Any]) -> dict[str, Any] | str:
    """The reconciler hook of the outcome store for a ``pending`` ``human.submit`` record some of whose events landed:
    the request is rolled forward and its outcome becomes the record's terminal state (``keep`` if it cannot be)."""
    requests = service.requests
    rid = (record.get("args_normalised") or {}).get("request_id")
    try:
        req = requests.read(rid) if requests is not None and isinstance(rid, str) else None
    except HostError:
        req = None
    if req is None or not req.get("submit"):
        return "keep"
    if req.get("outcome") is None:
        try:
            finish(service, requests, req)
        except (_Stored, HostError):
            pass
        req = requests.read(rid) or req
    outcome = req.get("outcome")
    if not outcome:
        return "keep"
    return {
        k: v
        for k, v in (
            ("step", outcome["state"]),
            ("result", outcome.get("result")),
            ("error", outcome.get("error")),
            ("exit", outcome["exit"]),
        )
        if v is not None
    }


def recover(service: WorkspaceService) -> list[tuple[str, str]]:
    """At start, before anything is served: every request whose signed events were being appended is rolled forward
    (§0.8.1), and the outcome record of its ``human.submit`` gets the same terminal state. Returns
    (request id, state)."""
    requests = service.requests
    out = []
    for record in requests.unfinished():
        try:
            roll_forward(service, requests, record)
            state = "committed"
        except (_Stored, HostError):
            state = "failed"
        out.append((record["id"], state))
    return out


def _sigs_digested(args: dict[str, Any]) -> dict[str, Any]:
    """``args`` with every event's ``sig`` string replaced by ``sha256:<hex>`` of it (a copy; ``args`` is unchanged)."""
    import hashlib

    from .auth import SIGNATURE_FIELDS

    key = SIGNATURE_FIELDS[0][1][0]  # the args key of the event list (not a store path; see auth.SIGNATURE_FIELDS)
    events = args.get(key)
    if type(events) is not list:
        return args
    out = []
    for e in events:
        if isinstance(e, dict) and type(e.get("sig")) is str:
            e = {**e, "sig": "sha256:" + hashlib.sha256(e["sig"].encode()).hexdigest()}
        out.append(e)
    return {**args, key: out}
