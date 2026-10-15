"""H12d-2 host ops of the reader-version handshake (orch-v2-host §0.8.6 item 9.10, 9.11): ``reader.declare`` and
``reader.list``.

* ``reader.declare {reader_decl}`` (``person_write``, column P; column R is H12d-3 with ``rd``): a write of host state
  only, no event. The declaration's device must be a current device of the calling person (their own local devices,
  ``orch keys reader declare``); it is verified with ``via:"declare"`` (the request's ±300 s window), the ``log`` KAT
  (a core's surface) and the device's certificate, then stored as the device's reader record under the store lock
  (:func:`orch.ops.readers.store_record`: idempotent on ``sha256(cj(o))``, latest wins by ``created_ms``). It answers
  the record now current, ``{device, reader, level, digest}``: a declaration that lost to a later one answers the
  stored one, so the caller sees that its ``digest`` was not taken.
* ``reader.list {}`` (a read, columns P and R): every current device with its reader record, ``{readers:[{device,
  person, dk_hash, reader, level, digest, decl}], required_reader}`` (``dk_hash``: what a declaration of the device
  binds, so ``orch keys reader declare`` can build one); a device without a record that verifies has ``reader``
  and ``level`` 0 and ``digest``/``decl`` null.

``required_reader`` in the reads comes from the flag and the durable mark only (no history scan on a read: the mark is
written at the first convenience append and by every reader-op check that scans the history).
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

__all__ = ["CAPS", "READER_HOST_OPS", "WRITE_OPS", "declare", "listing", "read_required", "serve"]

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

    if convenience.enabled(service.state_dir, service.workspace_id, service.host_uid):
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
        level = None if rec is None else rd.record_level(rec, service.workspace_id, dev, evidence)
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
    """``reader.declare`` for ``person`` (column P): verify, store, answer the current record."""
    from orch.identity.reader import DeclInvalid
    from orch.ops import readers as rd

    o = decl.get("o") if isinstance(decl, dict) else None
    dev_hex = o.get("device_id") if isinstance(o, dict) else None
    if not isinstance(dev_hex, str) or len(dev_hex) != 32:
        raise HostError("reader.decl_invalid", "shape: the declaration names no device")
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
                surfaces=("log",),
                now_ms=int(service.clock() * 1000),
            )
        except DeclInvalid as exc:
            raise HostError("reader.decl_invalid", exc.detail) from None
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
    level = rd.record_level(rec, service.workspace_id, dev, evidence)
    return {"device": device, "reader": rec["reader"], "level": level or 0, "digest": rec["digest"]}


def serve(call: Call) -> dict[str, Any]:
    """``reader.declare`` on the worker thread (after the column, P only): args, the person, an outcome record."""
    from .auth import Redactor
    from .outcomes import request_idem
    from .workspace import ok_result

    op = call.frame["op"]
    args = dict(call.frame["args"])
    errors = sorted(_VALIDATORS[op].iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        raise HostError("invalid.input", f"{op}: args are not valid")
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
        redactor=Redactor((call.frame.get("auth") or {}).get("grant")),
    )
