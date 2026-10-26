"""H12d-2 host ops of the reader-version handshake (orch-v2-host §0.8.6 item 9.10, 9.11): ``reader.declare`` and
``reader.list``.

* ``reader.declare {reader_decl}`` (``person_write`` in column P; ``device_signed`` in column R, H12d-3): a write of
  host state only, no event. **Column R** (a phone, :func:`declare_device`): the declaration's device and person
  must be the request's verified identity (its own device only), ``via:"declare"`` with the ``view`` surface (a
  phone's), no outcome record (it is idempotent on the declaration's digest and takes no idem); it is exempt from
  the ``rd`` check (it is how a device gets one). **Column P:** the declaration's device must be a current device
  of the calling person (their own local devices, ``orch keys reader declare``); it is verified with
  ``via:"declare"`` (the request's ±300 s window), the ``log`` KAT (a core's surface) and the device's certificate,
  then stored as the device's reader record under the store lock (:func:`orch.ops.readers.store_record`: idempotent
  on ``sha256(cj(o))``, latest wins by ``created_ms``). It answers the record now current, ``{device, reader, level,
  digest}``: a declaration that lost to a later one answers the stored one, so the caller sees that its ``digest``
  was not taken.
* ``reader.list {}`` (a read, columns P and R): every current device with its reader record, ``{readers:[{device,
  person, dk_hash, reader, level, digest, decl}], required_reader}`` (``dk_hash``: what a declaration of the device
  binds, so ``orch keys reader declare`` can build one); a device without a record that verifies has ``reader``
  and ``level`` 0 and ``digest``/``decl`` null.

``required_reader`` in the reads comes from the flag and the durable mark only (no history scan on a read: the mark is
written at the first convenience append and by every reader-op check that scans the history).

**Serving is bound to ``rd`` (H12d-3, §0.8.6 item 9.5):** :func:`check_rd` runs in ``Call.authorize`` for every
column-R request but ``reader.declare``, inside the op's own job. While ``required_reader`` is at least 1
(:func:`serving_required`: the flag, the mark, else the merged history, scanned once per store revision) the request's
``rd`` must name the device's current reader record (verified again against the device now) at that level, else
``sign.convenience_readers`` ``no_declaration`` / ``declaration_unknown`` / ``reader_too_old``. A replayed or resent
request is answered from its rid record by the bridge and never reaches this check (11A.4 "The rd field").
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from . import convenience
from .errors import HostError
from .visibility import Viewer

if TYPE_CHECKING:
    from .workspace import Call

__all__ = [
    "CAPS",
    "READER_HOST_OPS",
    "WRITE_OPS",
    "check_rd",
    "declare",
    "declare_device",
    "listing",
    "read_required",
    "serve",
    "serving_required",
]

CAPS = ["reader/1"]
READER_HOST_OPS: dict[str, int] = {"reader.declare": 1}
WRITE_OPS = frozenset({"reader.declare"})
_SCHEMAS = {
    "reader.declare": {
        "type": "object",
        "properties": {"reader_decl": {"type": "object"}},
        "required": ["reader_decl"],
        "additionalProperties": False,
    },
}
_VALIDATORS = {op: Draft202012Validator(s) for op, s in _SCHEMAS.items()}


def read_required(service: Any) -> int:
    """``required_reader`` for a read: 1 while the flag is on or the durable mark is set (no history scan)."""
    from orch.ops.readers import convenience_seen

    if convenience.flag_or_gate(service):  # §11A.17.4: the dev gate as the flag
        return 1
    return 1 if convenience_seen(service.state_dir, service.workspace_id) else 0


#: the fields of a ``reader.list`` row a phone (column R) gets (#513 review): no ``decl`` and no ``dk_hash``
PHONE_FIELDS = ("device", "person", "reader", "level")


def listing(service: Any, *, phone: bool = False) -> dict[str, Any]:
    """``reader.list``: every current device and what its record says now (verified again on every read). ``phone``
    (column R) trims each row to :data:`PHONE_FIELDS`."""
    from orch.ops import readers as rd
    from orch.store.readerrecs import read_record

    store = service.store
    state = store.state
    state_dir = Path(service.state_dir)
    rows = []
    for dev in rd.current_devices(state.workspace):
        rec = read_record(state_dir, service.workspace_id, dev.id[2:], service.host_uid)
        evidence = rd._evidence(state, dev.id)
        level = None if rec is None else rd._level_or_none(rec, service.workspace_id, dev, evidence)  # #514 N2
        ok = level is not None
        rows.append(
            {
                "device": dev.id,
                "person": dev.person,
                "dk_hash": _dk_hash(evidence),
                "reader": rec["reader"] if ok else 0,
                "level": level if ok else 0,
                "digest": rec["digest"] if ok else None,
                "decl": rec["decl"] if ok else None,
            }
        )
    if phone:
        rows = [{k: r[k] for k in PHONE_FIELDS} for r in rows]
    return {"readers": rows, "required_reader": read_required(service)}


def _dk_hash(evidence: Any) -> str | None:
    """``dk_hash`` of the device's certificate keys (what a declaration binds, 9.3), or ``None``."""
    from orch import crypto
    from orch.ops.readers import _cert_keys

    keys = _cert_keys((evidence or {}).get("device_cert"))
    return None if keys is None else crypto.b64u(crypto.dk_hash(*keys))


def device_levels(service: Any) -> dict[str, int]:
    """device -> counted level, for the current devices whose record verifies (``member.list`` answers ``null`` for
    the others, #505)."""
    from orch.ops import readers as rd

    lv = rd.levels(Path(service.state_dir), service.workspace_id, service.store.state, service.host_uid)
    return {d: v for d, v in lv.items() if v is not None}


def declare(service: Any, person: str, decl: Any) -> dict[str, Any]:
    """``reader.declare`` for ``person`` (column P): a current device of theirs, the ``log`` surface (a core)."""
    return _declare(service, person, decl, device=None, surfaces=("log",))


def declare_device(service: Any, identity: Any, decl: Any) -> dict[str, Any]:
    """``reader.declare`` in column R (H12d-3): the declaration of the requesting device itself (its device and
    person are the verified identity's), the ``view`` surface (a phone)."""
    return _declare(service, identity.person_id, decl, device=identity.device_id, surfaces=("view",))


def _declare(service: Any, person: str, decl: Any, *, device: str | None, surfaces: tuple[str, ...]) -> dict[str, Any]:
    """Verify (``via:"declare"``, the request's ±300 s), store under the store lock (latest wins, idempotent on
    ``sha256(cj(o))``), answer the record now current."""
    from orch.identity.reader import DeclInvalid
    from orch.ops import readers as rd

    o = decl.get("o") if isinstance(decl, dict) else None
    dev_hex = o.get("device_id") if isinstance(o, dict) else None
    if not isinstance(dev_hex, str) or len(dev_hex) != 32:
        raise HostError("reader.decl_invalid", "shape: the declaration names no device", detail={"reason": "shape"})
    if device is not None and "d_" + dev_hex != device:
        raise HostError("op.not_allowed", "a device declares only for itself")
    device = "d_" + dev_hex
    store = service.store
    with store.locked():
        store.refresh()
        state = store.state
        dev = state.workspace.devices.get(device)
        if dev is None or dev.removed or dev.revoked or dev.person != person:
            raise HostError("op.not_allowed", f"{device} is not a current device of yours")
        evidence = rd._evidence(state, device) or {}
        try:
            got = rd.verify_admission(
                decl,
                workspace_id=service.workspace_id,
                cert=evidence.get("device_cert"),
                person=person,
                via="declare",
                surfaces=surfaces,
                now_ms=int(service.clock() * 1000),
            )
        except DeclInvalid as exc:
            raise HostError("reader.decl_invalid", exc.detail, detail={"reason": exc.reason}) from None
        import time

        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(service.clock())))
        try:
            rec = rd.store_record(
                Path(service.state_dir),
                service.workspace_id,
                service.host_uid,
                rd.make_record(got, decl, person, "declare", stamp),
                incumbent_ok=lambda old: rd.record_level(old, service.workspace_id, dev, evidence) is not None,
            )
        except HostError:
            raise
        except Exception as exc:  # noqa: BLE001 - the record could not be written: nothing changed
            raise HostError(
                "custody.unsafe_mode", f"the reader record could not be written ({type(exc).__name__})"
            ) from None
    level = rd._level_or_none(rec, service.workspace_id, dev, evidence)
    return {"device": device, "reader": rec["reader"], "level": level or 0, "digest": rec["digest"]}


def serving_required(service: Any) -> int:
    """``required_reader`` for serving column R (9.5): 1 while the flag is on or the durable mark is set; else the
    merged history (and the cut tails) is scanned once per store revision (:meth:`orch.store.Store.revision`: the
    log heads and the ticket files' stat signatures; #555 review F1). A scan that finds a convenience event sets the
    mark, so after it the mark answers."""
    from orch.ops.readers import seen_or_history

    if read_required(service) >= 1:
        return 1
    store = service.store
    with store.locked():  # one revision for the key and the scan
        revision = store.revision()
        cached = getattr(service, "_rd_history", None)
        if cached is not None and cached[0] == revision:
            return int(cached[1])
        found = 1 if seen_or_history(store, Path(service.state_dir), service.workspace_id, service.host_uid) else 0
    service._rd_history = (revision, found)
    return found


def check_rd(service: Any, identity: Any) -> None:
    """H12d-3 (9.5): refuse a column-R request whose ``rd`` does not name the device's current reader record at the
    required level, while ``required_reader`` is at least 1. ``sign.convenience_readers`` with ``detail`` ``{reason,
    devices:[device]}`` (and ``required``/``declared`` for ``reader_too_old``, which only the relay refusal carries)."""
    from orch.ops import readers as rd
    from orch.store.readerrecs import read_record

    store = getattr(service, "store", None)
    if store is None:
        raise HostError("workspace.unavailable", "the workspace cannot be served")
    required = serving_required(service)
    if required < 1:
        return
    device = identity.device_id

    def refuse(reason: str, text: str, declared: int = 0) -> HostError:
        detail: dict[str, Any] = {"reason": reason, "devices": [device]}
        if reason == "reader_too_old":
            detail.update(required=required, declared=declared)
        return HostError(
            "sign.convenience_readers",
            f"{reason}: {text}",
            hint="update the orch app; it declares its reader level (reader.declare) and sends rd with every request",
            detail=detail,
        )

    if identity.rd is None:
        raise refuse("no_declaration", f"{device} sent no reader declaration digest (rd); this workspace requires "
                     f"reader level {required}")  # fmt: skip
    state = store.state
    dev = state.workspace.devices.get(device)
    rec = None if dev is None else read_record(Path(service.state_dir), service.workspace_id, device[2:],
                                               service.host_uid)  # fmt: skip
    level = None
    if rec is not None and dev is not None and not dev.removed and not dev.revoked and dev.person == identity.person_id:
        level = rd._level_or_none(rec, service.workspace_id, dev, rd._evidence(state, device))
    if level is None or not isinstance(rec, dict) or rec.get("digest") != identity.rd:
        raise refuse("declaration_unknown", f"{device}'s rd is not its current reader declaration; declare again")
    if level < required:
        raise refuse("reader_too_old", f"{device} declared reader level {level}, this workspace requires {required}",
                     level)  # fmt: skip


def serve(call: Call) -> dict[str, Any]:
    """``reader.declare`` on the worker thread (after the column): in P the args, the person, an outcome record; in R
    (H12d-3) the requesting device's own declaration (:func:`declare_device`)."""
    from .auth import Redactor
    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: args are not valid")
    if call.relay is not None:  # column R (H12d-3): the device's own declaration, no idem, no outcome record
        service = call.service
        if service.state == "unavailable" or service.store is None:
            raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
        return ok_result(declare_device(service, call.relay, args["reader_decl"]))
    idem = request_idem(op, call.frame.get("opts"))
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    viewer = Viewer.of(call)
    if viewer.person is None:
        raise HostError("op.not_allowed", f"{op} is a person's own")

    def apply(_intent: Any) -> dict[str, Any]:
        return ok_result(declare(service, viewer.person, args["reader_decl"]))

    return service.outcomes.execute(
        call.principal,
        str(idem),
        op,
        dict(args),
        apply,
        planned=None,
        session=(call.frame.get("auth") or {}).get("session"),
        redactor=Redactor((call.frame.get("auth") or {}).get("grant"), op),
    )
