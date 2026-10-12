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

Hand-off (H5b, F5): a request prepared with ``handoff:true`` gets a code, ``XXXX-XXXX`` in Crockford base32 (40 bits
from a CSPRNG, unique among this workspace's stored requests). The code **names** the request and authorises nothing:
``human.pending {code}`` finds it only for the same principal and person, in this workspace, while it is pending
(``prepared`` or ``bound``); a terminal request's code no longer resolves and is never issued again while the request is
kept. Failed code lookups are limited to 5 per minute **per connection** (``rate.limited``), so one connection's
failures never lock out another. ``human.get`` with ``signer`` binds the request to one device, once
(:mod:`orch.host.handoff` is ``orch sign CODE``).
"""

from __future__ import annotations

import base64
import copy
import secrets
import time
import weakref
from collections import deque
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from orch import canon

from . import review
from .errors import HostError
from .outcomes import Planned, request_idem
from .requests import EXPIRE_SECONDS, GRANT_EXPIRE_SECONDS, KEEP_SECONDS, PENDING, RequestStore, new_request_id
from .workspace import ok_result

if TYPE_CHECKING:
    from .auth import Redactor
    from .workspace import Call, WorkspaceService

__all__ = ["HUMAN_OPS", "HumanOps", "recover"]

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
_CODE = {"type": "string", "pattern": r"^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$"}

_DEVICE = {"type": "string", "pattern": r"^d_[0-9a-f]{32}$"}
_SIGNER = {
    "type": "object",
    "properties": {"device": _DEVICE, "auth": {"enum": ["passphrase", "secure-enclave"]}},
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
            "handoff": {"type": "boolean"},
        },
        "required": ["op", "args"],
        "additionalProperties": False,
    },
    "human.get": {
        "type": "object",
        "properties": {"request_id": _REQ, "signer": _SIGNER},
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
        "properties": {"code": _CODE, "request_id": _REQ},
        "maxProperties": 1,
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}
_GATE_TYPES = {"gate.approved", "gate.changes_requested", "verdict.given"}
_HASH = r"sha256:[0-9a-f]{64}"
#: Crockford base32 (no I, L, O, U): 8 characters are 40 bits
CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: failed ``human.pending {code}`` lookups allowed per connection per window (F5)
LOOKUP_FAILURES = 5
LOOKUP_WINDOW = 60.0


def new_code() -> str:
    """A hand-off code: 8 Crockford base32 characters from a CSPRNG (40 bits), shown as ``XXXX-XXXX``."""
    s = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"{s[:4]}-{s[4:]}"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def _expired(record: dict[str, Any], when: str = "") -> str:
    """The expiry message with the request's actual lifetime (10 minutes; a grant's: 4)."""
    minutes = round((record["expires"] - record["created"]) / 60)
    return f"the request expired{' ' + when if when else ''}: it lives {minutes} minutes after prepare"


def _validate(op: str, args: Any) -> None:
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def _store_error(exc: Any) -> HostError:
    return HostError(exc.code, exc.detail or exc.code)


def _unsigned(event: dict[str, Any], *, grant: bool) -> dict[str, Any]:
    out = {k: v for k, v in event.items() if k != "sig"}
    if grant and out.get("type") == "grant.issued":
        out.pop("secret_hash", None)
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


def _operate(types: list[str]) -> bool:
    from orch.model.authz import OPERATE

    return any(t in OPERATE for t in types)


# --- the ops ------------------------------------------------------------------------------------------------------


class HumanOps:
    """``human.*`` of one request, on the workspace worker thread (the dispatcher calls :meth:`serve`)."""

    ops = HUMAN_OPS

    def __init__(self, backend: Any) -> None:
        self.backend = backend  # the RegistryBackend: its context, file and state helpers
        #: per connection (the transport's Session): the times of its failed code lookups in the last window (F5).
        #: Only the workspace worker thread touches it; a closed connection's entry goes with its session.
        self._lookups: weakref.WeakKeyDictionary[Any, deque[float]] = weakref.WeakKeyDictionary()

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
        signer, handoff = args.get("signer"), bool(args.get("handoff", False))
        if handoff == (signer is not None):
            raise HostError("invalid.input", "human.prepare takes signer, or handoff:true without a signer")
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
        import orch.ops as ops

        service = call.service
        name = args["op"]
        operation = ops.get(name)
        inner = dict(args["args"])
        specs = self._specs(operation, call.frame, inner)
        digests, _ = self.backend.bind_files(call, None, specs)  # a {blob} must be the preparing principal's (F9)
        # the inner args as given (not normalised: a REF and its uid are two bindings, which only ever errs towards
        # idem.conflict), the person and the signer or hand-off; the session and file digests are added by binding_of
        bound = {
            "op": name,
            "args": inner,
            "signer": args.get("signer"),
            "handoff": bool(args.get("handoff", False)),
            "person": call.viewer,
        }
        session = (call.frame.get("auth") or {}).get("session")
        outcomes = service.outcomes
        begun = outcomes.begin(
            call.principal, idem, "human.prepare", bound, planned=(), files=digests, session=session, deferred=True
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
        import orch.ops as ops
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
        if signer is not None:
            check_device(store, person, signer["device"], now, operate=_operate(sorted(types)))
        operation = ops.get(name)
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
        )
        ctx = self.backend._context(call, None, False, files, prepare=capture)
        try:
            res = operation.handler(ctx, inner)
        except OrchError as e:
            raise host_error(e, operation) from None
        except StoreError as e:
            raise _store_error(e) from None
        if not capture.events:
            raise HostError("invalid.input", f"{name} has nothing to sign")
        logs = {c.log for c in capture.events}
        if len(logs) != 1:
            raise HostError("internal", f"{name} prepared events for more than one log")
        log = logs.pop()
        if signer is not None:
            check_device(
                store, person, signer["device"], now, operate=_operate([c.event["type"] for c in capture.events])
            )
        prepared, extras = self._build(call, name, log, capture, store, now)
        result = result_dict(res)
        template = {"data": result["data"], "key": result["key"], "hints": result["hints"]}
        if dry_run:
            return {"request": None, **prepared}
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
            "code": self._new_code(requests) if handoff else None,
            "state": "bound" if signer is not None else "prepared",
            "signer": signer,
            "created": now,
            "expires": now + (GRANT_EXPIRE_SECONDS if name == "grant" else EXPIRE_SECONDS),
            "prepared": prepared,
            "extras": extras,
            "result": template,
            "submit": None,
            "outcome": None,
        }
        requests.write(record)
        # the blobs the inner op used are held with the request: they live (and count against this UID) as long as it
        service.blobs.hold(call.principal, _blob_ids(specs), rid, record["expires"])
        view = self._view(record)
        if intent is not None:
            service.outcomes.commit(intent, view)
        return view

    def _specs(self, operation: Any, frame: dict[str, Any], inner: dict[str, Any]) -> dict[str, Any]:
        """The inner op's ``files``: inline ``{b64}`` or a ``{blob}``; a ``{digest}`` names no bytes, and a prepare
        has no earlier record to bind it to, so it is ``blob.invalid``."""
        specs = self.backend._file_specs(operation, frame, inner)
        for arg, spec in specs.items():
            if set(spec) not in ({"b64"}, {"blob"}):
                raise HostError("blob.invalid", f"files.{arg}: human.prepare takes the bytes (b64) or a blob")
        return specs

    @staticmethod
    def _new_code(requests: RequestStore) -> str:
        """A code no request stored in this workspace carries (terminal ones included, for the 24 h they are kept), so a
        code names one request only and is never issued twice while that request can be looked up."""
        taken = set()
        for rid in requests.ids():
            try:
                rec = requests.read(rid)
            except HostError:
                continue
            if rec is not None and rec.get("code"):
                taken.add(rec["code"])
        while True:
            code = new_code()
            if code not in taken:
                return code

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
            return {
                "kind": "gate",
                "gate": gate,
                "input": inp,
                "policy": _canon.canonical_policy(thaw(g.policy)),
                "texts": {sid: texts.get(sid, "") for sid in sorted(inp.get("sections") or {})},
                "ticket": ticket,
            }
        if len(events) == 1 and types[0] == "question.answered":
            qid = events[0]["question"]
            question = next((thaw(q) for q in view.fields["questions"] if q["id"] == qid), None)
            return {"kind": "question", "ticket": ticket, "question": question}
        material: dict[str, Any] = {"kind": "payload", "ticket": ticket}
        texts: dict[str, str] = {}
        for ev, extra in zip(events, extras, strict=True):
            for sid in ev.get("sections") or {}:
                body = extra.get("body") or {}
                texts[sid] = body.get(sid) or ""
        if texts:
            material["texts"] = texts
        before = dict(capture.before)
        for ev in events:
            if ev["type"] == "settings.changed":
                current = store.state.workspace
                for key in ev.get("set") or {}:
                    before.setdefault(key, thaw(getattr(current, key, None)))
        if before:
            material["before"] = before
        return material

    # -- reading and binding
    @staticmethod
    def _view(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "request": {
                "id": record["id"],
                "code": record.get("code"),
                "expires_at": _iso(record["expires"]),
                "state": record["state"],
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
        if state == "bound":
            if record["signer"] != signer:
                raise HostError("human.request_state", "the request is bound to another device")
            return self._view(record)
        store = call.service.store
        store.refresh()
        events = record["prepared"]["events"]
        check_device(
            store,
            record["person"],
            signer["device"],
            call.service.clock(),
            operate=_operate([e["type"] for e in events]),
        )
        for ev in events:  # the one change binding makes: null -> the signer's device and auth
            ev["actor"]["device"] = signer["device"]
            ev["auth"] = signer["auth"]
        record = {**record, "state": "bound", "signer": signer}
        requests.write(record)
        return self._view(record)

    def pending(self, call: Call, requests: RequestStore, args: dict[str, Any]) -> dict[str, Any]:
        """``human.pending`` (R6): this principal's requests, the one named by ``request_id``, or the pending one named
        by a hand-off ``code`` (F5). A failed submit carries ``error: {code}``, the catalog code only, never the
        message."""
        if "code" in args:
            return {"requests": [self._summary(r) for r in self._by_code(call, requests, args["code"])]}
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

    def _by_code(self, call: Call, requests: RequestStore, code: str) -> list[dict[str, Any]]:
        """The pending request of this principal and person that ``code`` names, in this workspace. After
        ``LOOKUP_FAILURES`` failed lookups in ``LOOKUP_WINDOW`` seconds on **this connection**, every code lookup on it
        answers ``rate.limited`` until the oldest failure leaves the window; other connections are not affected (F5)."""
        now = call.service.clock()
        failures = self._lookups.setdefault(call.session, deque())
        while failures and now - failures[0] >= LOOKUP_WINDOW:
            failures.popleft()
        if len(failures) >= LOOKUP_FAILURES:
            wait = max(1, int((failures[0] + LOOKUP_WINDOW - now) * 1000))
            raise HostError(
                "rate.limited",
                f"too many failed code lookups on this connection: at most {LOOKUP_FAILURES} per minute",
                retry_after_ms=wait,
            )
        found = []
        for rid in requests.ids():
            try:
                rec = requests.read(rid)
            except HostError:
                continue
            if (
                rec is not None
                and rec.get("code") == code
                and rec.get("principal") == call.principal
                and rec.get("person") == call.viewer
            ):
                rec = requests.expire(rec)
                if rec["state"] in PENDING:
                    found.append(rec)
        if not found:
            failures.append(now)
        return found

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        prepared = record["prepared"]
        ticket = prepared["material"].get("ticket")
        outcome = record.get("outcome") or {}
        out: dict[str, Any] = {
            "id": record["id"],
            "code": record.get("code"),
            "op": record["op"],
            "key": ticket["key"] if ticket else None,
            "created_at": _iso(record["created"]),
            "expires_at": _iso(record["expires"]),
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
            call.service.blobs.release(record["id"])  # the blobs it held go with it
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
        begun = outcomes.begin(call.principal, idem, "human.submit", args, planned=(), deferred=True, session=None)
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
        service.blobs.release(args["request_id"])  # submitted: the blobs it held are consumed
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
            git = service.capabilities().with_principal(call.principal).git
            self._judge(store, record, events, git)
            log = record["prepared"]["log"]
            view = store.ticket(log) if log != "workspace" else None
            key = view.key if view is not None else None
            outcomes = service.outcomes
            outcomes.plan(intent, [Planned(log, e["id"], e["type"], e["actor"]) for e in events], key)
            record = {**record, "submit": {"events": events, "outcome_key": intent.key}}
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
        """All events together with ``model.preview`` (signatures verified; the prepare-time judging in
        :func:`orch.ops.prepare.judge` set signatures aside), plus the D58 recheck of every signed source list."""
        from orch.model import Refusal, preview
        from orch.store import StoreError

        log = record["prepared"]["log"]
        state = store.state
        stamp = store.peek_stamp(log)
        seq, prev, at = stamp["seq"], stamp["prev"], stamp["at"]
        for ev in events:
            if "source_sha" in ev and log != "workspace":  # D58 first: git is read under the same lock
                recheck_sources(git, store, store.ticket(log), ev["source_sha"])
            probe = {**ev, "seq": seq, "prev": prev, "at": at}
            if "ws_seq" in stamp:
                probe["ws_seq"] = stamp["ws_seq"]
            r = preview(state, probe, log=log)
            if isinstance(r, Refusal):
                raise _store_error(StoreError.from_refusal(r))
            state, seq, prev = r, seq + 1, canon.event_head(probe)


def recheck_sources(git: Any, store: Any, view: Any, signed: list[dict[str, Any]]) -> None:
    """D58 at submit, under the store lock: each linked repo's head, read again through the host's git reader (under
    the caller's principal), must be the commit the decision signs, on the bound branch: ``gate.stale`` otherwise. A
    reader that cannot answer fails closed (``observe.unavailable``). The working tree's cleanliness is the worker's
    ``tree.check`` (H10), not checked here."""
    from orch.ops.caps import CapabilityDenied

    links = view.fields["links"]
    repos = store.state.workspace.repos
    for name in links["repos"]:
        branch = links["branches"].get(name)
        if not branch:
            continue
        try:
            path = git.repo_path(store.root, repos, name)
            head = git.head(path) if path is not None else None
            identity = git.identity(path, name) if head is not None else None
        except (CapabilityDenied, OSError) as exc:
            raise HostError("observe.unavailable", f"{name}: git cannot be read now ({exc})") from None
        if head is None or identity is None:
            raise HostError("observe.unavailable", f"{name}: git cannot be read now")
        entry = next((e for e in signed if e.get("repo") == identity), None)
        if entry is None or entry.get("ref") != f"refs/heads/{branch}" or entry.get("sha") != head:
            raise HostError("gate.stale", f"{name}: the code moved since the request was prepared; look again")


class _Stored(Exception):
    def __init__(self, error: HostError) -> None:
        self.error = error


def finish(service: WorkspaceService, requests: RequestStore, record: dict[str, Any]) -> dict[str, Any]:
    """Append the signed events of ``record`` that are not in the log yet, in order, under the store lock, then end
    the request ``submitted`` with its outcome. Used by ``human.submit`` and by start-up recovery (roll forward). A
    refusal after the first event landed ends the request ``failed`` naming the ``seq`` s that landed; it is never
    ``partial``. Returns the result; raises the stored error for a failed one."""
    from orch.store import StoreError

    store = service.store
    log = record["prepared"]["log"]
    events = record["submit"]["events"]
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
            for ev, extra in zip(events, record["extras"], strict=True):
                found = store.logged_event(log, ev["id"])
                if found is not None:
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
    now = service.clock()
    if error is not None:
        seqs = ", ".join(map(str, landed)) or "none"
        failed = HostError(error.code, f"{error.message} (landed: seq {seqs})"[:400])
        failed.exit, failed.retryable = error.exit, False
        outcome = {"state": "failed", "error": failed.error(), "exit": failed.exit, "seqs": landed}
        requests.write({**record, "state": "submitted", "done_at": now, "outcome": outcome})
        raise _Stored(failed) if landed else failed
    template = record.get("result") or {}
    view = store.ticket(log) if log != "workspace" else None
    result = ok_result(template.get("data") or {}, seq=max(landed))
    result["key"] = view.key if view is not None else template.get("key")
    result["hints"] = list(template.get("hints") or [])[:8]
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
    """The answer ``human.prepare`` gave for ``record``: a hand-off request (it has a code) is ``prepared``, any other
    one was bound to its signer at prepare."""
    return HumanOps._view({**record, "state": "prepared" if record.get("code") else "bound"})


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
