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

Until R3 no stream is ever registered, so the stream table (``gate.StreamTable``) is empty."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from orch.hubclient.errors import HubRefused

from ..errors import HostError
from . import gate

if TYPE_CHECKING:
    from ..workspace import Call

__all__ = [
    "REMOTE_CAPS", "REMOTE_OPS", "REMOTE_READS", "append_host", "attach", "attach_wait", "dev_ids", "on_commit",
    "serve", "streams",
]  # fmt: skip

LOG = logging.getLogger(__name__)

#: R3 (#1591): ``remote.attach`` (the dashboard's adapter: frames in and out, memory only) and ``remote.stream``
#: (what the signer is told about a stream) are reads of the log; ``remote.presence_used`` and
#: ``remote.session_end`` write it.
REMOTE_READS = frozenset({"remote.status", "remote.pending", "remote.sessions", "remote.attach", "remote.stream"})
REMOTE_CAPS = ["remote/1"]
REMOTE_OPS: dict[str, int] = {
    "remote.off": 1, "remote.code": 1, "remote.device_remove": 1, "remote.key_rotate": 1,
    "remote.status": 1, "remote.pending": 1, "remote.sessions": 1,
    "remote.attach": 1, "remote.stream": 1, "remote.presence_used": 1, "remote.session_end": 1,
}  # fmt: skip
_SCHEMAS: dict[str, dict[str, Any]] = {
    op: {"type": "object", "properties": {}, "additionalProperties": False} for op in REMOTE_OPS
}
_SID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
#: R3 T4: ``remote.off`` from an owner's own open session names it; the host takes the device from its session table
_SCHEMAS["remote.off"] = {
    "type": "object",
    "properties": {"stream_id": _SID},
    "additionalProperties": False,
}
_B64 = {"type": "string", "minLength": 1, "maxLength": 87400, "pattern": "^[A-Za-z0-9_-]+$"}
_SCHEMAS["remote.attach"] = {
    "type": "object",
    "properties": {
        "attach_id": {"type": "string", "pattern": "^at_[0-9a-f]{16}$"},
        "send": {
            "type": "array", "maxItems": 64,
            "items": {
                "type": "object", "properties": {"stream_id": _SID, "bytes": _B64},
                "required": ["stream_id", "bytes"], "additionalProperties": False,
            },
        },
        "close": {
            "type": "array", "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {"stream_id": _SID, "reason": {"enum": ["malformed", "remote.busy", "closed"]}},
                "required": ["stream_id", "reason"], "additionalProperties": False,
            },
        },
        "wait_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
    },
    "additionalProperties": False,
}  # fmt: skip
_SCHEMAS["remote.stream"] = {
    "type": "object",
    "properties": {"stream_id": _SID},
    "required": ["stream_id"],
    "additionalProperties": False,
}
_SCHEMAS["remote.session_end"] = _SCHEMAS["remote.stream"]
_SCHEMAS["remote.presence_used"] = {
    "type": "object",
    "properties": {
        "stream_id": _SID,
        "device_id": {"type": "string", "pattern": "^rd_[0-9a-f]{32}$"},
        "count": {"type": "integer", "minimum": 0, "maximum": 4294967295},
        "n": {"type": "integer", "minimum": 1, "maximum": 16},
    },
    "required": ["stream_id", "device_id", "count", "n"],
    "additionalProperties": False,
}
_SCHEMAS["remote.device_remove"] = {
    "type": "object",
    "properties": {"device_id": {"type": "string", "pattern": "^rd_[0-9a-f]{32}$"}},
    "required": ["device_id"],
    "additionalProperties": False,
}


# -- the stream table (gate.StreamTable, one per workspace) ----------------------------------------------------------

streams = gate.table  # ``remote.streams(service)``: open streams and the revokers R3 registers on them (design note 5)


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
        gate.on_commit(service, "workspace", event)  # the ONE place streams close; runs once per commit
        if typ in ("member.removed", "role.changed"):
            _drop_non_owner_devices(service)
        if typ in ("remote.device_added", "remote.device_removed", "remote.key_rotated"):
            from . import devices

            devices.export(service)
        if typ == "remote.device_added":
            from . import enrollment

            enrollment.on_added(service, event)
        if typ in ("remote.changed", "remote.device_added"):
            from . import policy

            policy.ensure_sweeper(service)
        if typ in _LINK_TYPES:
            from . import linkrun

            linkrun.refresh(service)  # the dedicated stream link follows the policy, the codes and the binding
    except Exception:  # noqa: BLE001
        LOG.exception("remote commit hook failed for %s", typ)


#: the workspace lines after which the stream link may have to start, stop or restart (R4 follow-up)
_LINK_TYPES = frozenset({
    "remote.changed", "remote.disabled", "remote.key_rotated", "remote.device_added", "remote.device_removed",
    "hub.joined", "hub.left",
})  # fmt: skip


def _drop_non_owner_devices(service: Any) -> None:
    """A person removed or no longer an owner: close their devices' streams and record ``remote.device_removed``
    (``reason: member``, H) for each, inside the same store hold. Idempotent: a device leaves the model with its line,
    and the line's own commit only re-closes nothing."""
    from . import devices

    with service.store.locked():
        owners = devices.owner_ids(service.store.state.workspace.members)
        for did, d in sorted((remote_state(service).get("devices") or {}).items()):
            if d["person"] in owners:
                continue
            gate.table(service).close_device(did, "revoked")
            append_host(service, "remote.device_removed", device_id=did, reason="member")


def attach(service: Any) -> None:
    """At host start (the store just opened): rewrite the device list from the model and start the sweeper if there
    is anything to sweep. Never raises."""
    try:
        from . import devices, policy

        with service.store.locked():
            devices.export(service)
        policy.ensure_sweeper(service)
        from . import linkrun

        with service.store.locked():
            linkrun.refresh(service)  # a workspace opened with remote access on (or bound with a code) dials at once
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
        return ok_result({"sessions": session_rows(service)})
    if op in ("remote.attach", "remote.stream", "remote.presence_used", "remote.session_end"):
        return _session_op(call, op, args, now_ms)
    if op == "remote.off":
        out = {"changed": _turn_off(call, args.get("stream_id"))}
    elif op == "remote.code":
        out = enrollment.issue(service, now_ms)
    elif op == "remote.device_remove":
        out = devices.remove(service, args["device_id"], "owner")
    else:  # remote.key_rotate
        out = wrk.rotate(service)
    _audit(call, op, **({"device_id": args["device_id"]} if "device_id" in args else {}))
    service.sync()
    return ok_result(out)


def _turn_off(call: Call, sid: str | None) -> bool:
    """The owner's off, or (``stream_id``) the off of an owner's open session: the device and the person come from the
    host's own session table, never from the request. A closed, unknown or another person's stream is ``no_stream``."""
    from . import policy, session

    if sid is None:
        return policy.turn_off(call.service, "owner")
    s = session.registry(call.service).get(sid)
    if s is None or s.state == "closed" or s.opened_ms is None or s.person != call.viewer:
        raise HostError("remote.no_stream", "no such remote session")
    return policy.turn_off(call.service, "device", device=s.ident)


def session_rows(service: Any) -> list[dict[str, Any]]:
    """The open streams; an opened session also shows its device name, person, level and since when."""
    from . import session

    reg = session.registry(service)
    rows = []
    for row in streams(service).open_streams():
        s = reg.get(row["stream_id"])
        live = s is not None and s.opened_ms is not None
        rows.append({
            **row, "name": s.name if live else None, "person": s.person if live else None,
            "level": s.level if live else None, "since_ms": s.opened_ms if live else None,
        })  # fmt: skip
    return rows


def _session_op(call: Call, op: str, args: dict[str, Any], now_ms: int) -> dict[str, Any]:
    from orch.crypto.encoding import EncodingError, b64u, unb64u

    from ..workspace import ok_result
    from . import adapter, counters, session

    service = call.service
    person = call.viewer
    if op == "remote.attach":
        ad = adapter.claim(service, person, args.get("attach_id"))  # type: ignore[arg-type]
        reg = session.registry(service)
        for item in args.get("close", []):
            s = reg.get(item["stream_id"])
            if s is not None and s.adapter is ad:
                streams(service).close(item["stream_id"], item["reason"])
        for item in args.get("send", []):
            s = reg.get(item["stream_id"])
            if s is None or s.adapter is not ad:
                continue  # a stream of no adapter of this connection: dropped (it may just have ended)
            try:
                s.send_app(unb64u(item["bytes"]))
            except EncodingError:
                streams(service).close(item["stream_id"], "malformed")
        return ok_result({"attach_id": ad.attach_id, "frames": ad.take(8)})
    sid = args["stream_id"]
    s = session.registry(service).get(sid)
    if op == "remote.session_end":
        with service.store.locked():
            if s is None or s.state == "closed" or not streams(service).close(sid, "revoked"):
                raise HostError("remote.no_stream", "no such remote session")
            if s.opened_ms is not None and not s.logged:
                s.logged = True
                append_host(service, "remote.session_closed", stream_id=sid, device_id=s.ident, reason="revoked")
        _audit(call, op, stream_id=sid)
        return ok_result({"ended": True})
    facts = session.stream_facts(service, sid, now_ms) if s is not None and s.person == person else {}
    if not facts:
        raise HostError("remote.no_stream", "no such remote session")  # closed, unknown or another person's
    if op == "remote.stream":
        pub = bytes.fromhex(facts["hh"])
        return ok_result({
            "workspace_id": facts["workspace_id"], "device_id": facts["device_id"], "person": facts["person"],
            "level": facts["level"], "hh": b64u(pub), "presence_kind": facts["presence_kind"],
            "presence_pub": facts["presence_pub"], "counter": counters.held(service, facts["device_id"]),
        })  # fmt: skip
    # remote.presence_used: the counter compare-and-set (FIDO2 only; a platform key has none) and the log line
    if args["device_id"] != facts["device_id"]:
        raise HostError("remote.no_stream", "no such remote session")
    try:
        with service.store.locked():
            if facts["presence_kind"] == "fido2":
                counters.cas(service, facts["device_id"], args["count"])
            append_host(service, "remote.presence", stream_id=sid, device_id=facts["device_id"], n=args["n"])
    except HubRefused:
        raise HostError("remote.presence_replay", "the presence counter did not rise") from None
    return ok_result({})


async def attach_wait(call: Call, result: dict[str, Any]) -> dict[str, Any]:
    """``remote.attach`` with ``wait_ms``: when the poll found nothing, park it (on the event loop, not the worker)
    until the adapter has a frame or the time is up; the lease is refreshed when it wakes."""
    import asyncio
    import contextlib

    from . import adapter

    wait = call.frame["args"].get("wait_ms", 0)
    data = result.get("data")
    ad = adapter.current(call.service)
    if (
        not isinstance(data, dict)
        or "frames" not in data
        or data["frames"]
        or not wait
        or ad is None
        or ad.attach_id != data["attach_id"]
    ):
        return result
    loop = asyncio.get_running_loop()
    ev = asyncio.Event()
    ad.wake = lambda: loop.call_soon_threadsafe(ev.set)
    try:
        if not ad.pending():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(ev.wait(), wait / 1000)
    finally:
        ad.wake = None
    ad.seen = call.service.clock()
    data["frames"] = ad.take(8)
    return result


def _pending_lookup(service: Any, pid: str) -> dict[str, Any] | None:
    from . import enrollment

    return enrollment.pending_source(service, pid)


def _plug() -> None:
    from .. import remotefacts

    remotefacts.set_pending_source(_pending_lookup)  # ``remote.device_confirm`` finds the pending device here
    from . import enrollment

    gate.CODES = enrollment.outstanding  # the gate's enroll check reads the outstanding codes


_plug()
