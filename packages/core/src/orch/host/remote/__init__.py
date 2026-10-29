"""Remote access, the host side (R2 #1590; design note r2-host-design.md, spec D84-D93, hub contract 10.2c).

The policy and the device set live in the workspace log (``model/remote.py`` is the authority); this package is what
the host does with them:

* :data:`REMOTE_OPS` / :func:`serve`: the owner's unsigned verbs (``remote.off``, ``remote.code``,
  ``remote.device_remove``, ``remote.key_rotate``) and reads (``remote.status``, ``remote.pending``,
  ``remote.sessions``). Column P and a workspace owner only. Turning remote access ON and confirming a device are
  person ops with the owner proof (``ops/person_ops.py``), not here.
* :func:`on_commit`: the ONE revocation point. ``Store.on_commit`` runs under the workspace store lock; every line that
  ends access (``remote.disabled``, ``remote.changed``, ``remote.key_rotated``, a full ``hub.joined``,
  ``remote.device_removed``) closes streams there, inside the hold, so no request can slip between the log and the
  close. It also rewrites the signed device list and resolves a pending enrollment.
* ``policy`` (effective policy, the one off path, the expiry sweeper), ``devices`` (lookup, the WSK-signed export),
  ``enrollment`` (codes, the verify seam R3 calls), ``wrk`` (the key).

Until R3 no stream is ever registered, so the stream table here is empty."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..errors import HostError

if TYPE_CHECKING:
    from ..workspace import Call

__all__ = [
    "REMOTE_OPS", "REMOTE_READS", "StreamTable", "append_host", "attach", "dev_ids", "on_commit", "serve", "streams",
]  # fmt: skip

LOG = logging.getLogger(__name__)

REMOTE_READS = frozenset({"remote.status", "remote.pending", "remote.sessions"})
REMOTE_OPS: dict[str, int] = {
    "remote.off": 1, "remote.code": 1, "remote.device_remove": 1, "remote.key_rotate": 1,
    "remote.status": 1, "remote.pending": 1, "remote.sessions": 1,
}  # fmt: skip
_SCHEMAS: dict[str, dict[str, Any]] = {
    op: {"type": "object", "properties": {}, "additionalProperties": False} for op in REMOTE_OPS
}
_SCHEMAS["remote.device_remove"] = {
    "type": "object",
    "properties": {"device_id": {"type": "string", "pattern": "^rd_[0-9a-f]{32}$"}},
    "required": ["device_id"],
    "additionalProperties": False,
}


# -- the stream table seam (T2b's ``gate.StreamTable``; this one stands in until it is merged) -----------------------


class StreamTable:
    """Stand-in for T2b's ``gate.StreamTable`` (same names: ``register``, ``close_all``, ``close_device``,
    ``take_closing``) until ``host/remote/gate.py`` is merged; ``streams`` and ``_revoke`` prefer the gate when it
    exists. Open streams and the revokers R3 registers on them (design note section 5)."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._open: dict[str, tuple[str, Callable[[str], None]]] = {}
        self._closed: list[tuple[str, str]] = []

    def register(
        self, stream_id: str, device_id: str, revoke: Callable[[str], None] | None = None, kind: str = "session"
    ) -> None:
        with self._mu:
            self._open[stream_id] = (device_id, revoke or (lambda reason: None))

    def _close(self, picked: list[str], reason: str) -> list[str]:
        with self._mu:
            gone = [(sid, self._open.pop(sid)) for sid in picked if sid in self._open]
            self._closed.extend((sid, reason) for sid, _ in gone)
        for sid, (_, revoke) in gone:
            try:
                revoke(reason)
            except Exception:  # noqa: BLE001 - one failing revoker never keeps another stream open
                LOG.exception("remote revoker failed for %s", sid)
        return [sid for sid, _ in gone]

    def close_all(self, reason: str) -> list[str]:
        with self._mu:
            picked = list(self._open)
        return self._close(picked, reason)

    def close_device(self, device_id: str, reason: str) -> list[str]:
        with self._mu:
            picked = [sid for sid, (dev, _) in self._open.items() if dev == device_id]
        return self._close(picked, reason)

    def take_closing(self) -> list[tuple[str, str]]:
        with self._mu:
            out, self._closed = self._closed, []
        return out

    def open_streams(self) -> list[dict[str, str]]:
        with self._mu:
            return [{"stream_id": sid, "device_id": dev} for sid, (dev, _) in sorted(self._open.items())]


def _gate() -> Any:
    try:
        from . import gate
    except ImportError:
        return None
    return gate


def streams(service: Any) -> Any:
    """The workspace's stream table: T2b's ``gate.table(service)`` when that module is there, else the stand-in."""
    gate = _gate()
    if gate is not None:
        return gate.table(service)
    table = getattr(service, "_remote_streams", None)
    if table is None:
        table = service._remote_streams = StreamTable()  # noqa: SLF001
    return table


# -- host-written log lines ------------------------------------------------------------------------------------------


def append_host(service: Any, typ: str, **payload: Any) -> dict[str, Any]:
    """Append one host (H) line to the workspace log under the store lock; ``on_commit`` runs inside that hold."""
    store = service.store
    with store.locked():
        return dict(store.append({"type": typ, "actor": {"kind": "host"}, **payload}, log="workspace").event)


def remote_state(service: Any) -> dict[str, Any]:
    """The model's remote state at this position (a plain dict; ``{}`` until the first ``remote.*`` line)."""
    from orch.store.render import thaw

    return dict(thaw(service.store.state.workspace.remote) or {})


def dev_ids(service: Any) -> list[str]:
    return sorted(remote_state(service).get("devices", {}))


def _audit(call: Call, event: str, **more: Any) -> None:
    from .. import securitylog

    s = call.service
    securitylog.record(
        s.state_dir, s.host_uid, s.clock(), event, workspace_id=s.workspace_id, principal=call.principal, **more
    )


# -- the commit hook -------------------------------------------------------------------------------------------------


def on_commit(service: Any, log: str, event: dict[str, Any]) -> None:
    """``Store.on_commit`` (under the store lock). Never raises: a failure is logged, the commit stands."""
    if log != "workspace":
        return
    typ = event.get("type")
    try:
        _revoke(service, typ, event)
        if typ in ("remote.device_added", "remote.device_removed"):
            from . import devices

            devices.export(service)
        if typ == "remote.device_added":
            from . import enrollment

            enrollment.on_added(service, event)
        if typ in ("remote.changed", "remote.device_added"):
            from . import policy

            policy.ensure_sweeper(service)
    except Exception:  # noqa: BLE001
        LOG.exception("remote commit hook failed for %s", typ)


def _revoke(service: Any, typ: Any, event: dict[str, Any]) -> None:
    gate = _gate()
    if gate is not None:
        gate.on_commit(service, "workspace", event)  # T2b owns the rules when its module is merged
        return
    table = None
    if typ in ("remote.disabled", "remote.changed", "remote.key_rotated", "remote.device_removed", "hub.joined"):
        table = streams(service)
    if typ == "remote.disabled" or (typ == "hub.joined" and event.get("mode") == "full"):
        table.close_all("remote.off")
    elif typ in ("remote.changed", "remote.key_rotated"):
        table.close_all("revoked")  # any change, also a narrowing: sessions reopen under the new rules
    elif typ == "remote.device_removed":
        table.close_device(event["device_id"], "revoked")


def attach(service: Any) -> None:
    """At host start (the store just opened): rewrite the device list from the model and start the sweeper if there
    is anything to sweep. Never raises."""
    try:
        from . import devices, policy

        with service.store.locked():
            devices.export(service)
        policy.ensure_sweeper(service)
    except Exception:  # noqa: BLE001
        LOG.exception("remote attach failed")


# -- the ops ---------------------------------------------------------------------------------------------------------


def _owner(call: Call) -> str:
    """Column P only (``op.not_allowed`` for an agent, a relay or anything else) and the person must be an owner."""
    if call.column.name != "P" or call.viewer is None:
        raise HostError("op.not_allowed", "a person at this computer does this; an agent never changes remote access")
    member = call.service.store.state.workspace.members.get(call.viewer)
    if member is None or member.role != "owner":
        raise HostError("role.denied", "remote access is for workspace owners")
    return call.viewer


def _validate(op: str, args: Any) -> None:
    from jsonschema import Draft202012Validator

    errors = sorted(Draft202012Validator(_SCHEMAS[op]).iter_errors(args), key=lambda e: list(map(str, e.path)))
    if errors:
        where = ".".join(map(str, errors[0].absolute_path))
        raise HostError("invalid.input", f"{op}: {where or 'args'} is not valid")


def serve(call: Call) -> dict[str, Any]:
    from ..workspace import ok_result
    from . import devices, enrollment, policy, wrk

    op = call.frame["op"]
    args = dict(call.frame["args"])
    _validate(op, args)
    if call.frame.get("files"):
        raise HostError("blob.invalid", f"{op} takes no files")
    if (call.frame.get("opts") or {}).get("dry_run"):
        raise HostError("invalid.input", f"dry_run is not supported by {op}")
    service = call.service
    if service.state == "unavailable" or service.store is None:
        raise HostError("workspace.unavailable", f"the workspace cannot be served: {service.cause}")
    if op not in REMOTE_READS and service.state == "degraded":
        raise HostError("workspace.degraded", f"the workspace is read-only: {service.cause}")
    service.store.refresh()
    _owner(call)
    now_ms = int(service.clock() * 1000)
    if op == "remote.status":
        return ok_result(policy.status(service, now_ms))
    if op == "remote.pending":
        return ok_result({"pending": enrollment.pending_list(service, now_ms)})
    if op == "remote.sessions":
        return ok_result({"sessions": list(getattr(streams(service), "open_streams", list)())})
    if op == "remote.off":
        out = {"changed": policy.turn_off(service, "owner")}
    elif op == "remote.code":
        out = enrollment.issue(service, now_ms)
    elif op == "remote.device_remove":
        out = devices.remove(service, args["device_id"], "owner")
    else:  # remote.key_rotate
        out = wrk.rotate(service)
    _audit(call, op, **({"device_id": args["device_id"]} if "device_id" in args else {}))
    service.sync()
    return ok_result(out)


def _pending_lookup(service: Any, pid: str) -> dict[str, Any] | None:
    from . import enrollment

    return enrollment.pending_source(service, pid)


def _plug() -> None:
    from .. import remotefacts

    remotefacts.set_pending_source(_pending_lookup)  # ``remote.device_confirm`` finds the pending device here
    gate = _gate()
    if gate is not None:
        from . import enrollment

        gate.CODES = enrollment.outstanding  # the gate's enroll check reads the outstanding codes


_plug()
