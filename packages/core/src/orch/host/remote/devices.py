"""The remote device set as the host serves it (R2 #1590, design note sections 1, 2; hub contract 10.2c "The device
list"): :func:`lookup` over the model, :func:`export` (the WSK-signed ``remote.device_list`` file, a derived export:
the gate and every request read the model, never this file), :func:`check_list` (the reader's rules, what a device
or the host applies to a list it is handed) and :func:`remove`."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from orch.canon.jcs import JcsError, dumps
from orch.crypto import suite
from orch.crypto.encoding import EncodingError, unb64u
from orch.crypto.labels import L
from orch.hubclient.custodial_shape import text_ok
from orch.hubclient.errors import HubRefused

from ..errors import HostError

__all__ = ["LABEL", "build", "check_list", "export", "list_path", "lookup", "owner_ids", "remove"]

LABEL = L["sig_remote_devices"]
_ENTRY = (
    "device_id", "name", "person", "transport_pub", "presence_pub", "presence_kind", "level_cap", "expires_ms",
    "added_ms", "confirmed_by",
)  # fmt: skip
_LIFE_MAX = 366 * 86_400_000
_MAX_SAFE = 2**53 - 1


def list_path(service: Any) -> Path:
    from . import wrk

    return wrk.state_dir(service) / "devices.json"


def owner_ids(members: Mapping[str, Any]) -> frozenset[str]:
    """The persons that are workspace owners right now (the model's roster)."""
    return frozenset(p for p, m in members.items() if m.role == "owner")


def lookup(
    remote: Mapping[str, Any] | None, device_id: str, now_ms: int, owners: Collection[str]
) -> dict[str, Any] | None:
    """The listed, unexpired device ``device_id`` from the model's ``ws.remote`` whose person is a current owner
    (``owners``, :func:`owner_ids`), or ``None`` (``remote.device``). A removed or demoted person's devices never
    pass, whether or not the commit hook has removed them from the list yet."""
    d = ((remote or {}).get("devices") or {}).get(device_id)
    if d is None or not now_ms < d["expires_ms"] or d["person"] not in owners:
        return None
    return {"device_id": device_id, **d}


def build(service: Any, remote: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    """The contract ``remote.device_list`` object of the model's devices of current owners (sorted by id)."""
    owners = owner_ids(service.store.state.workspace.members)
    return {
        "v": 2, "suite": 2, "kind": "remote.device_list", "hv": 0, "workspace_id": service.workspace_id,
        "version": remote.get("device_version", 0), "issued_ms": now_ms,
        "devices": [
            {k: ({"device_id": did, **d})[k] for k in _ENTRY}
            for did, d in sorted((remote.get("devices") or {}).items())
            if d["person"] in owners
        ],
    }  # fmt: skip


def export(service: Any) -> bool:
    """Rewrite ``remote/devices.json`` from the model: the list signed with ``WSK`` under the devices label, 0600,
    atomic. Nothing is written while the model has no device version yet (version 0). Called under the store lock."""
    from .. import hubops
    from ..custody import ensure_private_dir, write_private
    from . import remote_state, wrk

    r = remote_state(service)
    if not r.get("device_version"):
        return False
    o = build(service, r, int(service.clock() * 1000))
    _, sign = hubops._wsk_sign(service)  # noqa: SLF001 - the one WSK signer of the host
    from orch.crypto.encoding import b64u

    sig = sign(LABEL + dumps(o))
    ensure_private_dir(wrk.state_dir(service), service.host_uid)
    write_private(list_path(service), {"o": o, "sig": b64u(sig)})
    return True


def _no(code: str = "malformed") -> HubRefused:
    return HubRefused(code)


def _nat(x: Any, lo: int = 0) -> bool:
    return type(x) is int and lo <= x <= _MAX_SAFE


def _b64_ok(x: Any, n: int) -> bool:
    try:
        unb64u(x, n)
    except EncodingError:
        return False
    return True


def _pub_ok(x: Any) -> bool:
    try:
        suite.validate_public_key(unb64u(x, 65))
    except Exception:  # noqa: BLE001 - any invalid point
        return False
    return True


def _ident(x: Any, prefix: str) -> bool:
    return (
        type(x) is str
        and x.startswith(prefix)
        and len(x) == len(prefix) + 32
        and all(c in "0123456789abcdef" for c in x[len(prefix) :])
    )


_CHECKS = {
    "device_id": lambda x: _ident(x, "rd_"),
    "name": lambda x: text_ok(x, 64),
    "person": lambda x: _ident(x, "p_"),
    "transport_pub": lambda x: _b64_ok(x, 32),
    "presence_pub": _pub_ok,
    "presence_kind": lambda x: x in ("platform", "fido2"),
    "level_cap": lambda x: x in ("view", "work", "terminals"),
    "expires_ms": _nat,
    "added_ms": _nat,
    "confirmed_by": lambda x: _ident(x, "p_"),
}


def check_list(signed: Any, wsk_pub: bytes, workspace_id: str, held_version: int) -> dict[str, Any]:
    """The rules a reader applies to a signed ``remote.device_list`` (contract 10.2c; vectors ``device_list_*``): the
    exact shape, suite 2, the signature of ``WSK`` under the devices label, sorted unique ids, no key used twice, life
    at most 366 days, this workspace, and never older than the version already held. Raises ``HubRefused``."""
    if type(signed) is not dict or set(signed) != {"o", "sig"} or type(signed["o"]) is not dict:
        raise _no()
    o = signed["o"]
    if o.get("kind") != "remote.device_list":
        raise _no()
    if type(o.get("suite")) is not int or o["suite"] != 2:
        raise _no("wrong_suite")
    if type(o.get("v")) is not int or type(o.get("hv")) is not int or o["v"] != 2 or o["hv"] != 0:
        raise _no()
    try:
        body = dumps(o)
        sig = unb64u(signed["sig"], 64)
    except (JcsError, EncodingError):
        raise _no() from None
    if not suite.verify(wsk_pub, sig, LABEL + body):
        raise _no("bad_signature")
    top = {"v", "suite", "kind", "hv", "workspace_id", "version", "issued_ms", "devices"}
    if set(o) != top or type(o["workspace_id"]) is not str or not _ident(o["workspace_id"], ""):
        raise _no()
    if (
        not _nat(o["version"], 1)
        or not _nat(o["issued_ms"])
        or type(o["devices"]) is not list
        or len(o["devices"]) > 64
    ):
        raise _no()
    keys: set[str] = set()
    ids = []
    for d in o["devices"]:
        if type(d) is not dict or set(d) != set(_CHECKS) or not all(f(d[k]) for k, f in _CHECKS.items()):
            raise _no()
        if not d["added_ms"] < d["expires_ms"] <= d["added_ms"] + _LIFE_MAX:
            raise _no()
        for k in ("transport_pub", "presence_pub"):
            if d[k] in keys:
                raise _no()
            keys.add(d[k])
        ids.append(d["device_id"])
    if ids != sorted(set(ids)):
        raise _no()
    if o["workspace_id"] != workspace_id:
        raise _no("other_workspace")
    if o["version"] < held_version:
        raise _no("stale_version")
    return o


def remove(service: Any, device_id: str, reason: str) -> dict[str, Any]:
    """``remote.device_removed`` (H) under the store lock: the commit hook closes that device's streams and rewrites
    the list. A device that is not listed is ``invalid.input``."""
    from . import append_host, remote_state

    with service.store.locked():
        if device_id not in (remote_state(service).get("devices") or {}):
            raise HostError("invalid.input", "no such remote device")
        append_host(service, "remote.device_removed", device_id=device_id, reason=reason)
    return {"device_id": device_id, "removed": True}
