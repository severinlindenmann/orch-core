"""The routes a hub uses at its relay account (protocol.md §10.8.1, §10.8.2; phase 4, D10 / relay E2).

Pure functions: the route table with its body checks (what the relay implements, byte for byte), the request builders
the hub client uses, and the push payload seal (v2 §5.3 construction, purpose ``hub-push``). No I/O, no clock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import suite as S
from .canon import CanonError, b64u, cj, loads_strict, unb64u, unhex
from .hub import Refused
from .phase4 import parse_relay_target
from .spaces import _seal_aad, _seal_key

PURPOSE_HUB_PUSH = "hub-push"
PUSH_TYPES = ("prepare", "needs_you")
ENVS = ("sandbox", "production")
PRIORITIES = ("high", "normal")
MAX_TTL_S = 86_400
MAX_OUTER = 3072  # bytes of cj(outer), the APNs payload field (same cap as the per-workspace send)
MAX_MAILBOX_C = 16 * 1024  # decoded bytes of one mailbox message
MAILBOX_TTL = (60, 7 * 86_400)
_TOPIC = re.compile(r"[A-Za-z0-9.-]{1,155}")
_HEX32 = re.compile(r"[0-9a-f]{32}")


def _hex(x: Any) -> bool:
    try:
        unhex(x, 16)
        return True
    except CanonError:
        return False


def _obj(raw: bytes, fields: set[str]) -> dict[str, Any]:
    try:
        o = loads_strict(raw)
    except CanonError:
        raise Refused("malformed") from None
    if type(o) is not dict or set(o) != fields or o.get("v") != 2 or type(o["v"]) is not int:
        raise Refused("malformed")
    return o


def outer_check(outer: Any, hub_id: str, channel_id: str) -> bytes:
    """The APNs ``o`` field of a push (§10.8.1): exactly ``{v, hub, ch, id, c}`` naming this hub and channel."""
    if type(outer) is not dict or set(outer) != {"v", "hub", "ch", "id", "c"}:
        raise Refused("malformed")
    if type(outer["v"]) is not int or outer["v"] != 2 or outer["hub"] != hub_id or outer["ch"] != channel_id:
        raise Refused("malformed")
    if not _hex(outer["id"]) or type(outer["c"]) is not str or not outer["c"]:
        raise Refused("malformed")
    try:
        unb64u(outer["c"])
    except CanonError:
        raise Refused("malformed") from None
    raw = cj(outer)
    if len(raw) > MAX_OUTER:
        raise Refused("too_large")
    return raw


def push_exempt(method: str, segs: list[str]) -> bool:
    """§10.8.1 (E5R-1): the requests a relay without a complete push configuration still routes. The mailbox purge is
    the hub's revocation notice (§10.8.4), a security fact that never depends on APNs; the hub cut-over routes are
    §10.8.3's. Decided by method and path shape only, before ``{ch}`` or the body is looked at."""
    if method == "DELETE" and len(segs) == 2 and segs[0] == "mailbox":
        return True
    return len(segs) == 3 and segs[0] == "workspaces" and segs[2] == "cutover"


def route(method: str, target: str, hub_id: str, body: bytes, push_configured: bool = True) -> dict[str, Any]:
    """Match a (canonical, §10.8) target and check the body. Returns ``{route, channel_id[, id]}``; an unknown path or
    method is ``not_found``, a body that is not exactly the route's shape ``malformed`` (``too_large`` for an oversize
    message). The relay has authenticated the request (§10.8 steps 1-6) before it calls this. ``push_configured``
    False: every request that is not ``push_exempt`` is ``not_found`` (after the §10.8 target checks); the exempt
    ones follow the ordinary order of refusals."""
    parse_relay_target(target, hub_id)
    path, _, query = target.partition("?")
    segs = path.split("/")[4:]  # after /v2/hubs/<hub_id>
    if not push_configured and not push_exempt(method, segs):
        raise Refused("not_found")
    if query:
        raise Refused("malformed")  # none of these routes takes a query
    kind = segs[0] if segs else ""
    if kind == "push" and len(segs) in (2, 3):
        ch = segs[1]
        if len(segs) == 2 and method == "PUT":
            o = _obj(body, {"v", "env", "topic", "token"})
            try:
                tok = unb64u(o["token"])
            except CanonError:
                raise Refused("malformed") from None
            if o["env"] not in ENVS or type(o["topic"]) is not str or not _TOPIC.fullmatch(o["topic"]):
                raise Refused("malformed")
            if not 32 <= len(tok) <= 100:
                raise Refused("malformed")
            return {"route": "push_put", "channel_id": _chan(ch)}
        if len(segs) == 2 and method == "DELETE":
            _empty(body)
            return {"route": "push_delete", "channel_id": _chan(ch)}
        if len(segs) == 3 and segs[2] == "send" and method == "POST":
            o = _obj(body, {"v", "outer", "priority", "ttl_s"})
            if o["priority"] not in PRIORITIES or type(o["ttl_s"]) is not int or not 0 <= o["ttl_s"] <= MAX_TTL_S:
                raise Refused("malformed")
            outer_check(o["outer"], hub_id, _chan(ch))
            return {"route": "push_send", "channel_id": ch}
    if kind == "mailbox" and len(segs) in (2, 3):
        ch = _chan(segs[1])
        if len(segs) == 2 and method == "DELETE":
            _empty(body)
            return {"route": "mailbox_purge", "channel_id": ch}
        if len(segs) == 3 and _HEX32.fullmatch(segs[2]):
            mid = segs[2]
            if method == "PUT":
                o = _obj(body, {"v", "c", "ttl_s"})
                if type(o["ttl_s"]) is not int or not MAILBOX_TTL[0] <= o["ttl_s"] <= MAILBOX_TTL[1]:
                    raise Refused("malformed")
                try:
                    c = unb64u(o["c"])
                except CanonError:
                    raise Refused("malformed") from None
                if not c:
                    raise Refused("malformed")
                if len(c) > MAX_MAILBOX_C:
                    raise Refused("too_large")
                return {"route": "mailbox_put", "channel_id": ch, "id": mid}
            if method in ("GET", "DELETE"):
                _empty(body)
                return {"route": "mailbox_get" if method == "GET" else "mailbox_delete", "channel_id": ch, "id": mid}
    if kind == "workspaces" and len(segs) == 3 and segs[2] == "cutover" and method in ("PUT", "GET", "DELETE"):
        ws = _chan(segs[1])  # 32 lower-case hex, else malformed
        if method == "PUT":
            o = _obj(body, {"v", "state"})
            if o["state"] != "cut_over":
                raise Refused("malformed")  # a hub only confirms; it never names, redirects or sets pending
            return {"route": "cutover_put", "workspace_id": ws}
        _empty(body)
        if method == "DELETE":
            raise Refused("not_allowed")  # a hub never reverses a cut-over (403)
        return {"route": "cutover_get", "workspace_id": ws}
    raise Refused("not_found")


# --- §10.8.3 the cut-over state at the relay (relay E3) ---------------------------------------------------------------

CUTOVER_STATES = ("pending", "cut_over")


def cutover_view(row: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"state": "direct"} if row is None else {"state": row["state"], "hub_id": row["hub_id"]}


def cutover_workspace(
    row: Mapping[str, Any] | None, method: str, body: bytes, registered_hubs: frozenset[str] = frozenset()
) -> dict[str, Any] | None:
    """The workspace's own request (its session, WSK): the new row (None = direct). ``PUT {v: 2, hub_id, state}``
    names a hub (``pending``) or cuts over at once (``cut_over``); naming a hub never weakens a cut-over
    (``cut_over``, 403: DELETE first); ``DELETE`` always returns to the direct path."""
    if method == "DELETE":
        _empty(body)
        return None
    if method != "PUT":
        raise Refused("not_found")
    o = _obj(body, {"v", "hub_id", "state"})
    if type(o["hub_id"]) is not str or not _HEX32.fullmatch(o["hub_id"]):
        raise Refused("malformed")
    if o["state"] not in CUTOVER_STATES:
        raise Refused("malformed")
    if o["hub_id"] not in registered_hubs:
        raise Refused("unknown_hub")  # 403, to the workspace only: it may name only a hub with an account here
    if row is not None and row["state"] == "cut_over" and o["state"] == "pending":
        raise Refused("cut_over")
    return {"state": o["state"], "hub_id": o["hub_id"]}


def cutover_hub(row: Mapping[str, Any] | None, hub_id: str, route_name: str) -> dict[str, Any]:
    """The hub's request after :func:`route`: only for a workspace that named exactly this hub (unknown, not named and
    another hub's are one answer, ``other_hub`` 403). ``cutover_put`` confirms ``pending`` -> ``cut_over``
    (idempotent); ``cutover_get`` reads."""
    if row is None or row["hub_id"] != hub_id:
        raise Refused("other_hub")
    if route_name == "cutover_put":
        row = {"state": "cut_over", "hub_id": hub_id}
    return cutover_view(row)


def _chan(x: str) -> str:
    if not _HEX32.fullmatch(x):
        raise Refused("malformed")
    return x


def _empty(body: bytes) -> None:
    if body != b"":
        raise Refused("malformed")


# --- request builders (the hub client) --------------------------------------------------------------------------------


def base(hub_id: str) -> str:
    return f"/v2/hubs/{hub_id}"


def push_put_request(hub_id: str, channel_id: str, env: str, topic: str, token: bytes) -> tuple[str, str, bytes]:
    body = cj({"v": 2, "env": env, "topic": topic, "token": b64u(token)})
    return "PUT", f"{base(hub_id)}/push/{channel_id}", body


def push_delete_request(hub_id: str, channel_id: str) -> tuple[str, str, bytes]:
    return "DELETE", f"{base(hub_id)}/push/{channel_id}", b""


def push_send_request(
    hub_id: str, channel_id: str, msg_id: str, sealed: str, priority: str = "high", ttl_s: int = 3600
) -> tuple[str, str, bytes]:
    outer = {"v": 2, "hub": hub_id, "ch": channel_id, "id": msg_id, "c": sealed}
    body = cj({"v": 2, "outer": outer, "priority": priority, "ttl_s": ttl_s})
    return "POST", f"{base(hub_id)}/push/{channel_id}/send", body


def mailbox_put_request(
    hub_id: str, channel_id: str, msg_id: str, sealed: str, ttl_s: int = 86_400
) -> tuple[str, str, bytes]:
    return "PUT", f"{base(hub_id)}/mailbox/{channel_id}/{msg_id}", cj({"v": 2, "c": sealed, "ttl_s": ttl_s})


def mailbox_get_request(hub_id: str, channel_id: str, msg_id: str) -> tuple[str, str, bytes]:
    return "GET", f"{base(hub_id)}/mailbox/{channel_id}/{msg_id}", b""


def mailbox_delete_request(hub_id: str, channel_id: str, msg_id: str | None = None) -> tuple[str, str, bytes]:
    tail = f"/{msg_id}" if msg_id else ""
    return "DELETE", f"{base(hub_id)}/mailbox/{channel_id}{tail}", b""


def cutover_request(hub_id: str, workspace_id: str, method: str = "PUT") -> tuple[str, str, bytes]:
    body = cj({"v": 2, "state": "cut_over"}) if method == "PUT" else b""
    return method, f"{base(hub_id)}/workspaces/{workspace_id}/cutover", body


# --- the sealed push payload (§10.8.2) --------------------------------------------------------------------------------


def push_payload(kind: str, ts_ms: int, ref: str | None) -> dict[str, Any]:
    """The plaintext the phone's NSE opens. A hint to open the app, never an authority (§10.8.2)."""
    if (
        kind not in PUSH_TYPES
        or type(ts_ms) is not int
        or not 0 <= ts_ms < 2**53
        or (ref is not None and not _hex(ref))
    ):
        raise Refused("malformed")
    return {"v": 2, "hv": 0, "kind": "hub.push", "type": kind, "ts_ms": ts_ms, "ref": ref}


def _aad(hub_id: str, msg_id: str) -> bytes:
    return _seal_aad(PURPOSE_HUB_PUSH, unhex(msg_id, 16), 0, unhex(hub_id, 16))


def seal_push(
    payload: Mapping[str, Any], *, hub_id: str, channel_id: str, msg_id: str, recipient_pub: bytes, eph_seed: bytes
) -> str:
    """``sealed = b64u(eph_pub || AES-256-GCM(K, 0-nonce, cj(payload), AAD))``: v2 §5.3 with purpose ``hub-push``,
    recipient id = ``channel_id``, object id = ``msg_id``, epoch 0, extra = ``hub_id``. ``recipient_pub`` is the
    channel's registered push key ``push_pub`` (§6.4, B amendment), never its signing key; ``eph_seed``: a fresh key
    per seal."""
    if not S.valid_public_key(recipient_pub) or not _hex(channel_id):
        raise Refused("malformed")
    plaintext = cj(dict(payload))
    eph = S.key_from_seed(eph_seed)
    eph_pub = S.public_bytes(eph)
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), recipient_pub))
    key = _seal_key(ss, eph_pub, recipient_pub, PURPOSE_HUB_PUSH, channel_id)
    return b64u(eph_pub + AESGCM(key).encrypt(bytes(12), plaintext, _aad(hub_id, msg_id)))


def open_push(
    sealed: str, *, hub_id: str, channel_id: str, msg_id: str, recipient_priv: ec.EllipticCurvePrivateKey
) -> dict[str, Any]:
    """What the phone does (the reference for the NSE, with its push key). Wrong key, hub, channel or message id:
    ``malformed``."""
    try:
        raw = unb64u(sealed)
        eph_pub, ct = raw[:65], raw[65:]
        if len(ct) < 16 or not S.valid_public_key(eph_pub):
            raise Refused("malformed")
        ss = recipient_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
        if ss == bytes(32):
            raise Refused("malformed")
        key = _seal_key(ss, eph_pub, S.public_bytes(recipient_priv), PURPOSE_HUB_PUSH, channel_id)
        o = loads_strict(AESGCM(key).decrypt(bytes(12), ct, _aad(hub_id, msg_id)))
    except InvalidTag, CanonError, ValueError:
        raise Refused("malformed") from None
    if (
        type(o) is not dict
        or set(o) != {"v", "hv", "kind", "type", "ts_ms", "ref"}
        or o["kind"] != "hub.push"
        or o["v"] != 2
        or o["hv"] != 0
    ):
        raise Refused("malformed")
    return push_payload(o["type"], o["ts_ms"], o["ref"])


__all__ = [
    "ENVS",
    "PRIORITIES",
    "PUSH_TYPES",
    "mailbox_delete_request",
    "mailbox_get_request",
    "mailbox_put_request",
    "open_push",
    "outer_check",
    "push_delete_request",
    "push_exempt",
    "push_payload",
    "push_put_request",
    "push_send_request",
    "route",
    "seal_push",
]
