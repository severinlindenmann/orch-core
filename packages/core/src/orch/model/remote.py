"""Remote access in the workspace log (R2, #1590; hub-remote spec D84-D93, hub contract 10.2c).

The owner's remote policy and the remote device list are folded from signed lines of the workspace log, so the model is
the one authority: a host file is only ever an export of this state. ``remote.changed`` and ``remote.device_added`` are
the owner's own acts at the workspace (a person line with the remote owner proof of the backup key, never a custodial
line); every other ``remote.*`` line is the host's record and can only reduce power. A full ``hub.joined`` ends remote
access here too (:func:`hub_full`), whatever the premises answer.
"""

from __future__ import annotations

from typing import Any

from orch.crypto import suite
from orch.crypto.encoding import EncodingError, unb64u

from .codes import Code, Refusal
from .types import WsCore, ts
from .verifier import Verifier

__all__ = [
    "DEVICES_MAX", "DEVICE_LIFE_MAX_MS", "HUB_MODES", "LEVELS", "PREMISES", "PRESENCE_KINDS", "device_fp",
    "hub_full", "hub_mode_of", "is_on", "on_line", "remote_apply",
]  # fmt: skip

LEVELS = ("view", "work", "terminals")
PRESENCE_KINDS = ("platform", "fido2")
PREMISES = ("yes", "no")
HUB_MODES = ("none", "selective", "full")
#: contract 10.2c "The device list": ``added_ms < expires_ms <= added_ms + 366 d``, at most 64 entries
DEVICE_LIFE_MAX_MS = 366 * 86_400_000
DEVICES_MAX = 64
_FP_LABEL = b"orch/v2/remote-device-fp|"
_KIND_BYTE = {"platform": 1, "fido2": 2}


def device_fp(presence_kind: str, transport_pub: bytes, presence_pub: bytes) -> bytes:
    """Contract 10.2c: ``SHA-256("orch/v2/remote-device-fp|" || kind (1) || transport_pub (32) || presence_pub (65))``
    (vectors ``device_fp_*``). Raises ``ValueError`` for anything malformed."""
    if presence_kind not in _KIND_BYTE or type(transport_pub) is not bytes or len(transport_pub) != 32:
        raise ValueError("malformed")
    try:
        suite.validate_public_key(presence_pub)
    except Exception as exc:  # noqa: BLE001 - any invalid point
        raise ValueError("malformed") from exc
    return suite.sha256(_FP_LABEL + bytes([_KIND_BYTE[presence_kind]]) + transport_pub + presence_pub)


def _no(why: str) -> Refusal:
    return Refusal(Code.REMOTE_INVALID, why)


def hub_mode_of(ws: WsCore) -> str:
    """``none`` | ``selective`` | ``full``: the model's own record of the hub at this position."""
    h = ws.hub
    if h and h.get("invalid"):
        return "full"  # an unreadable hub step counts as full (fail closed, like every reader of the signing mode)
    return h["mode"] if h and h.get("status") == "joined" else "none"


def is_on(ws: WsCore, now_ms: int) -> bool:
    """Is remote access on at ``now_ms``: the policy says on and its end date is not reached."""
    r = ws.remote
    return bool(r) and r.get("on") is True and (r.get("until_ms") is None or now_ms < r["until_ms"])


def on_line(ws: WsCore) -> bool:
    return bool(ws.remote) and ws.remote.get("on") is True


def hub_full(ws: WsCore) -> None:
    """A full ``hub.joined`` turns remote access off (D93, stricter than the spec's "if yes"): a switch to full, a
    leave and rejoin, another hub. Called by ``hub_joined`` after it applied the line."""
    if ws.remote and ws.remote.get("on") is True:
        _set(ws, on=False, off_reason="hub_full")


def _set(ws: WsCore, **changes: Any) -> None:
    """Apply a change to ``ws.remote`` and raise the remote counter (``version``): every state-changing line (an owner's
    policy or device, a disable, a removal, a key rotation, a full hub) moves it, so an owner proof, which names the
    counter it was made at, can never be replayed at a later position (R2 replay rule)."""
    cur = ws.remote
    ws.remote = {"devices": {}, "device_version": 0, **cur, **changes, "version": cur.get("version", 0) + 1}


def _bind(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """The position and single-use rules of an owner's remote line: the signed ``remote_v`` is the model's current
    counter; the proof's request id was never used before (a proof the host consumed once stays consumed)."""
    if e["remote_v"] != ws.remote.get("version", 0):
        return _no("stale_remote")
    proof = e.get("owner_proof")
    rid = proof["o"].get("request_id") if isinstance(proof, dict) and isinstance(proof.get("o"), dict) else None
    if rid is not None and rid in ws.remote.get("used_requests", ()):
        return _no("proof_reused")
    return None


def _used(ws: WsCore, e: dict[str, Any]) -> list[str]:
    proof = e.get("owner_proof")
    rid = proof["o"].get("request_id") if isinstance(proof, dict) and isinstance(proof.get("o"), dict) else None
    return [*ws.remote.get("used_requests", ()), *([rid] if rid is not None else [])]


def _owner_person(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    """Owner only, and only the owner's own person line: never a hub user acting through a channel."""
    a = e["actor"]
    if a.get("kind") != "person" or "via" in a or e.get("auth") == "hub":
        return _no("not_custodial")
    m = ws.members.get(a["id"])
    if m is None or m.role != "owner":
        return Refusal(Code.ROLE_DENIED, f"{e['type']} is owner only")
    return None


def _proof(ws: WsCore, e: dict[str, Any], v: Verifier, dev: bool) -> Refusal | None:
    """The remote owner proof (contract 10.2c), whatever the ``auth``: the actor's ``PK`` from the roster and the
    append time of the host-signed line, never a body field. A verifier without the method refuses (fail closed)."""
    check = getattr(v, "verify_hub_owner_proof", None)
    if check is None:
        return _no("owner_proof: unavailable")
    actor = e["actor"]["id"]
    member = ws.members.get(actor)
    code = check(
        e, workspace_id=ws.workspace_id, actor_person_id=actor.removeprefix("p_"),
        pk_pub=None if member is None else member.pk_pub, prior=None, at_ms=ts(e["at"]) * 1000, dev=dev,
    )  # fmt: skip
    return None if code is None else _no(f"owner_proof: {code}")


def _changed(ws: WsCore, e: dict[str, Any], v: Verifier, dev: bool) -> Refusal | None:
    if (r := _owner_person(ws, e)) is not None or (r := _proof(ws, e, v, dev)) is not None:
        return r
    if (r := _bind(ws, e)) is not None:
        return r
    at_ms = ts(e["at"]) * 1000
    if e["on"] is not True or e["level"] not in LEVELS or e["premises"] not in PREMISES:
        return _no("malformed")
    if e["hub_mode"] != hub_mode_of(ws):
        return _no("hub_mode_mismatch")  # the line names the hub mode the owner saw; it must be the model's own
    if e["premises"] == "yes" and e["hub_mode"] == "full":
        return _no("needs_selective")
    until = e["until_ms"]
    if until is None and (e["premises"] == "yes" or e["level"] == "terminals"):
        return _no("until_required")
    if until is not None and until <= at_ms:
        return _no("until_past")
    _set(
        ws, on=True, level=e["level"], until_ms=until, premises=e["premises"], agents_risk=e["agents_risk"],
        hub_mode=e["hub_mode"], off_reason=None, used_requests=_used(ws, e),
    )  # fmt: skip
    return None


def _disabled(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    if not on_line(ws):
        return _no("not_on")  # off already: nothing to record, and no line ever turns anything on
    if e["by"] == "device" and e.get("device") not in ws.remote.get("devices", {}):
        return _no("device_unknown")
    _set(ws, on=False, off_reason=e["by"])
    return None


def _added(ws: WsCore, e: dict[str, Any], v: Verifier, dev: bool) -> Refusal | None:
    if (r := _owner_person(ws, e)) is not None or (r := _proof(ws, e, v, dev)) is not None:
        return r
    if (r := _bind(ws, e)) is not None:
        return r
    cur = ws.remote
    devices = dict(cur.get("devices", {}))
    at_ms = ts(e["at"]) * 1000
    if e["person"] != e["actor"]["id"]:
        return _no("person_mismatch")  # R2 enrolls the owner's own devices only
    if not at_ms < e["expires_ms"] <= at_ms + DEVICE_LIFE_MAX_MS:
        return _no("device_life")
    if len(devices) >= DEVICES_MAX:
        return _no("full")
    if e["device_id"] in devices or any(
        d["transport_pub"] == e["transport_pub"] or d["presence_pub"] == e["presence_pub"] for d in devices.values()
    ):
        return _no("device_exists")  # ids unique and no key used twice (contract 10.2c)
    if e["device_id"] in cur.get("seen_devices", ()) or e["code_id"] in cur.get("seen_codes", ()):
        return _no("ever_seen")  # a device id or an enrollment code is used once in the life of the log, even removed
    try:
        want = device_fp(e["presence_kind"], unb64u(e["transport_pub"], 32), unb64u(e["presence_pub"], 65))
    except (ValueError, EncodingError):
        return _no("malformed")
    if e["fingerprint"] != want.hex():
        return _no("fingerprint")
    devices[e["device_id"]] = {
        "name": e["name"], "person": e["person"], "presence_kind": e["presence_kind"],
        "transport_pub": e["transport_pub"], "presence_pub": e["presence_pub"], "level_cap": e["level_cap"],
        "expires_ms": e["expires_ms"], "added_ms": at_ms, "confirmed_by": e["actor"]["id"],
        "fingerprint": e["fingerprint"], "code_id": e["code_id"],
    }  # fmt: skip
    _set(
        ws,
        devices=devices,
        device_version=cur.get("device_version", 0) + 1,
        used_requests=_used(ws, e),
        seen_devices=[*cur.get("seen_devices", ()), e["device_id"]],
        seen_codes=[*cur.get("seen_codes", ()), e["code_id"]],
    )
    return None


def _removed(ws: WsCore, e: dict[str, Any]) -> Refusal | None:
    devices = dict(ws.remote.get("devices", {}))
    if e["device_id"] not in devices:
        return _no("device_unknown")
    del devices[e["device_id"]]
    _set(ws, devices=devices, device_version=ws.remote.get("device_version", 0) + 1)
    return None


def remote_apply(ws: WsCore, e: dict[str, Any], v: Verifier, *, dev: bool) -> Refusal | None:
    """One ``remote.*`` workspace line: refuses it or applies it to ``ws.remote``."""
    t = e["type"]
    if t == "remote.changed":
        return _changed(ws, e, v, dev)
    if t == "remote.device_added":
        return _added(ws, e, v, dev)
    if t == "remote.disabled":
        return _disabled(ws, e)
    if t == "remote.device_removed":
        return _removed(ws, e)
    if t == "remote.key_rotated":
        # R2 review L3: a new key removes every device in the SAME line (they pinned the old one): no window
        _set(ws, wrk_fp=e["wrk_fp"], devices={}, device_version=ws.remote.get("device_version", 0) + 1)
        return None
    return None  # code_issued, code_burned, session_opened, session_closed: records, no state
