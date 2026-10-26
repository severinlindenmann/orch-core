"""The phone side of the hub contract (protocol.md §6.4 and §10.8.4; phase 4, B stream, orch-core #861–#866).

Pure reference functions, no I/O and no clock:

- :func:`parse_pair_link`: the operator's pairing link / QR (``orch://hub-pair/...``);
- :func:`session_ref`, :func:`verify_channel_auth`: a channel key binds a pairing session of its user to itself, which
  the hub then turns into a channel session (12 h, spec §7);
- :func:`verify_channel_push`: a channel key names the push key its hints are sealed to (§10.8.2);
- :func:`channel_request_bytes`, :func:`check_channel_request`, :func:`channel_route`: a channel's own read of its
  mailbox at the relay (closes §10.8.1 open point O1).
"""

from __future__ import annotations

import ipaddress
import re
import struct
from collections.abc import Mapping
from typing import Any

from . import suite as S
from .canon import CanonError, b64u, loads_strict, unb64u, unhex
from .hub import (
    _TS as TS_RE,
)
from .hub import (
    CLOCK_SKEW_MS,
    Refused,
    _dev,
    _envelope,
    _fields,
    _lp16,
    _signature,
    _under_card,
    verify_card,
    verify_channel_cert,
)
from .labels import L

# --- §6.4 the pairing link ---------------------------------------------------------------------------------------------

PAIR_PREFIX = "orch://hub-pair/"
PAIR_MAX = 512
_HOST = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
_AUTHORITY = re.compile(rf"(?P<host>{_HOST}|\[::1\])(?::(?P<port>[1-9][0-9]{{0,4}}))?")
_LOOPBACK = ("localhost", "127.0.0.1", "[::1]")
_NETS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)  # private + CGNAT: plain http like the stored-origin rule (contracts/origin_http.json)
_IPV4 = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}")


def _origin_host(origin: str) -> str:
    rest = origin[len("http://") :]
    return rest.split("]")[0] + "]" if rest.startswith("[") else rest.split(":")[0]


def _plain_http_host(host: str) -> bool:
    if host in _LOOPBACK:
        return True
    if _IPV4.fullmatch(host) is None:
        return False
    try:
        ip = ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return any(ip in n for n in _NETS)


_FRAGMENT = re.compile(
    r"h1\.(?P<hub>[0-9a-f]{32})\.(?P<user>[0-9a-f]{32})\.(?P<code>[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})"
)


def parse_pair_link(text: Any, *, dev_mode: bool) -> dict[str, str]:
    """``orch://hub-pair/<authority>#h1.<hub_id>.<user_id>.<code>`` → ``{api, hub_id, user_id, code}``.

    Strict: ASCII, at most 512 bytes, lower-case host (``https://`` is implied; a loopback host is plain ``http://`` and accepted only by a dev
    verifier, ``dev_mode``; a private or CGNAT IPv4 host (10/8, 172.16/12, 192.168/16, 100.64/10) is plain ``http://`` too), an optional port 1–65535 without leading zero, no userinfo, path, query or second ``#``; the fragment
    exactly ``h1`` and lower-case hex ids and the hub's 12-hex pairing code. Anything else is ``malformed``. The
    ``hub_id`` is the pin (§6.4): the app verifies the card it fetches from ``api`` against it."""
    if type(text) is not str or not text.isascii() or len(text) > PAIR_MAX or not text.startswith(PAIR_PREFIX):
        raise Refused("malformed")
    rest = text[len(PAIR_PREFIX) :]
    authority, sep, fragment = rest.partition("#")
    if not sep:
        raise Refused("malformed")
    a, f = _AUTHORITY.fullmatch(authority), _FRAGMENT.fullmatch(fragment)
    if a is None or f is None:
        raise Refused("malformed")
    if a["port"] is not None and int(a["port"]) > 65535:
        raise Refused("malformed")
    if a["host"] == "[::1]" and a["port"] is None:
        raise Refused("malformed")
    if a["host"] in _LOOPBACK and dev_mode is not True:
        raise Refused("malformed")
    scheme = "http://" if _plain_http_host(a["host"]) else "https://"
    return {"api": scheme + authority, "hub_id": f["hub"], "user_id": f["user"], "code": f["code"]}


def fingerprint(hub_id: str) -> str:
    """What the app shows at pairing and the admin console shows next to the QR: the hub id in 8 groups of 4."""
    unhex(hub_id, 16)
    return " ".join(hub_id[i : i + 4] for i in range(0, 32, 4))


def verify_api_origin(
    signed: Any,
    held: Mapping[str, Any],
    *,
    link_api: str,
    now_ms: int,
    dev_mode: bool,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The phone's check, before it sends a password (§6.4, L2B-1): ``hub.api_origin`` verifies under the card it holds
    (pinned to the link's ``hub_id``; §6.1 rules: ``revoked``, ``bad_signature``, ``other_hub``, ``stale_op_cert``, dev
    marker), ``issued_ms`` is not in the future (``cert_invalid``), a plain-http origin only at a dev verifier
    (``malformed``), and ``origin`` is exactly the link's API origin → else ``other_origin``. A phishing host can relay
    the object, but it names the real origin, so a link to another host fails here."""
    o = _under_card(signed, "hub.api_origin", held, dev_mode, revoked_op_certs)
    if o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("cert_invalid")
    if o["origin"].startswith("http://") and dev_mode is not True and _origin_host(o["origin"]) in _LOOPBACK:
        raise Refused("malformed")
    if o["origin"] != link_api:
        raise Refused("other_origin")
    return {"origin": o["origin"]}


# --- §6.4 channel sessions -------------------------------------------------------------------------------------------

AUTH_WINDOW_MS = 120_000
CHANNEL_SESSION_MS = 12 * 3_600_000  # spec §7: a channel session lives up to 12 h after the password sign-in


def session_ref(token: Any) -> str:
    """``b64u(H("orch/v2/hub-session-ref|" || token))``: names the pairing session inside ``hub.channel_auth`` without
    putting the bearer token itself into a signed object. ``token`` is the ASCII token as the hub issued it."""
    if type(token) is not str or not token or not token.isascii() or len(token) > 256:
        raise Refused("malformed")
    return b64u(S.sha256(L["h_hub_session_ref"] + token.encode("ascii")))


def verify_channel_auth(
    signed: Any,
    *,
    hub_id: str,
    session_user: str,
    session_token: str,
    channels: Mapping[str, Mapping[str, Any]],
    now_ms: int,
    dev_mode: bool,
) -> dict[str, str]:
    """The hub's check of ``POST /api/v1/session/channel`` (§6.4), in order. The request carries a live **pairing**
    session of ``session_user`` (checked by the caller, ``no_session``). ``channels``: every channel the hub ever
    registered, ``channel_id -> {user_id, kind, key_pub (b64u), expires_ms (current cert), revoked (bool)}``.

    Codes: ``malformed``/``wrong_suite`` (envelope); ``unknown_channel`` (no channel of this session's user by that id:
    the session is authenticated, and another user's id answers the same); ``bad_signature`` (not that channel's
    key); ``malformed`` (fields); ``other_hub``; ``other_user``; ``not_allowed`` (not a phone channel);
    ``challenge_invalid`` (``session_ref`` is not this session's); ``stale_timestamp``; dev marker; then, only for a
    holder of the key, ``channel_revoked`` and ``cert_expired``."""
    o = _envelope(signed, "hub.channel_auth")
    cid = o.get("channel_id")
    if type(cid) is not str or not re.fullmatch(r"[0-9a-f]{32}", cid):
        raise Refused("malformed")
    ch = channels.get(cid)
    if ch is None or ch.get("user_id") != session_user:
        raise Refused("unknown_channel")
    try:
        key = unb64u(ch["key_pub"], 65)
    except CanonError, KeyError, TypeError:
        raise Refused("internal") from None
    _signature(signed, L["sig_hub_channel_auth"], key)
    _fields(o, "hub.channel_auth")
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["user_id"] != session_user:
        raise Refused("other_user")
    if ch.get("kind") != "phone":
        raise Refused("not_allowed")
    if o["session_ref"] != session_ref(session_token):
        raise Refused("challenge_invalid")
    if abs(now_ms - o["ts_ms"]) > AUTH_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    _dev(o, dev_mode)
    if ch.get("revoked") is not False:
        raise Refused("channel_revoked")
    if type(ch.get("expires_ms")) is not int or now_ms >= ch["expires_ms"]:
        raise Refused("cert_expired")
    return {"channel_id": cid, "user_id": session_user}


def verify_channel_push(
    signed: Any,
    *,
    hub_id: str,
    channel_id: str,
    key_pub: bytes,
    now_ms: int,
    dev_mode: bool,
    held_issued_ms: int | None = None,
) -> dict[str, Any]:
    """The hub's check of the ``push_key`` of ``PUT /api/v1/channels/{channel_id}/push`` (§6.4), in order, for the
    channel the session is bound to (``key_pub``: its current certificate's key). Codes: envelope; ``bad_signature``;
    fields; ``other_hub``; ``other_user`` (another channel); ``stale_timestamp``; dev marker; ``malformed`` when the push
    key is the channel key itself (one key, one use); ``replay`` when ``issued_ms`` is not newer than the held push
    key's (an old registration cannot be replayed back)."""
    o = _envelope(signed, "hub.channel_push")
    _signature(signed, L["sig_hub_channel_push"], key_pub)
    _fields(o, "hub.channel_push")
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["channel_id"] != channel_id:
        raise Refused("other_user")
    if abs(now_ms - o["issued_ms"]) > AUTH_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    _dev(o, dev_mode)
    if unb64u(o["push_pub"], 65) == key_pub:
        raise Refused("malformed")
    if held_issued_ms is not None and o["issued_ms"] <= held_issued_ms:
        raise Refused("replay")
    return {"push_pub": o["push_pub"], "issued_ms": o["issued_ms"]}


# --- §10.8.4 a channel's own mailbox read at the relay ---------------------------------------------------------------

CHANNEL_WINDOW_MS = 120_000
CHANNEL_NONCE_RETENTION_MS = 240_000
CHANNEL_METHODS = ("GET", "DELETE")
CHANNEL_TARGET_MAX = 256
CHANNEL_CERT_HEADER_MAX = 4096
MAILBOX_LIST_MAX = 256  # the per-channel cap of §10.8.1
_SEG = re.compile(r"[A-Za-z0-9._~-]+")
_HEX32 = re.compile(r"[0-9a-f]{32}")


def _parse_channel_target(target: Any) -> tuple[str, str, list[str]]:
    """``/v2/hub-channels/<hub_id>/<channel_id>/...``: ASCII, unreserved segments, no query, no empty, ``.`` or ``..``
    segment, no trailing ``/``. Returns ``(hub_id, channel_id, rest)``; anything else is ``malformed``."""
    if type(target) is not str or not target.isascii() or len(target) > CHANNEL_TARGET_MAX or "?" in target:
        raise Refused("malformed")
    segs = target.split("/")
    if segs[0] != "" or len(segs) < 5 or segs[1:3] != ["v2", "hub-channels"]:
        raise Refused("malformed")
    for s in segs[1:]:
        if not _SEG.fullmatch(s) or s in (".", ".."):
            raise Refused("malformed")
    if not _HEX32.fullmatch(segs[3]) or not _HEX32.fullmatch(segs[4]):
        raise Refused("malformed")
    return segs[3], segs[4], segs[5:]


def channel_request_bytes(
    hub_id: str, channel_id: str, ts_ms: int, nonce: bytes, method: str, target: str, body: bytes
) -> bytes:
    """``"orch/v2/hub-channel-relay-req|" || suite || hub_id (16) || channel_id (16) || u64(ts_ms) || nonce (16) ||
    lp16(METHOD) || lp16(target) || H(body)`` (the §10.8 layout with the channel in place of the op cert)."""
    if type(ts_ms) is not int or not 0 <= ts_ms < 1 << 64 or type(nonce) is not bytes or len(nonce) != 16:
        raise Refused("malformed")
    for part in (method, target):
        if type(part) is not str or not part.isascii():
            raise Refused("malformed")
    if type(body) is not bytes:
        raise Refused("malformed")
    return (
        L["sig_hub_channel_relay_req"]
        + bytes([S.SUITE_ID])
        + unhex(hub_id, 16)
        + unhex(channel_id, 16)
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + S.sha256(body)
    )


def check_channel_request(
    req: Mapping[str, Any], st: Mapping[str, Any], *, now_ms: int, dev_mode: bool
) -> dict[str, Any]:
    """The relay's check of a channel request (§10.8.4), in order. ``req`` = {method, target, headers, body (bytes)};
    ``st`` = {hub_cards: {hub_id: signed card}, revoked_op_certs: [..], revoked_channels: {hub_id: [channel ids the
    hub revoked by its signed mailbox purge, §10.8.1]}, nonces: {"<hub>/<ch>": {nonce hex: ts_ms}}}.

    1 header syntax, method, canonical target, the certificate's encoding → ``malformed``; 2 ``|now − ts| ≤ 120 s`` →
    ``stale_timestamp``; 3 the held card of the target's hub, verified again → ``unknown_hub``; 4 the certificate under
    that card (§6.2: ``bad_signature``, ``other_hub``, ``stale_op_cert``, ``revoked``, ``cert_invalid``,
    ``cert_expired``, dev marker) and naming the target's channel → ``other_user``; 5 the signature under its key →
    ``bad_channel_signature``; 6 the channel not revoked by its hub → ``channel_revoked`` (after the signature, so only
    the key holder learns it); 7 the nonce unseen for this (hub, channel) → ``replay``. Returns the hub, the channel
    and the nonce to record for :data:`CHANNEL_NONCE_RETENTION_MS`."""
    h: dict[str, Any] = {}
    for k, v in req["headers"].items():
        if str(k).lower() in h:  # L2B-9: one spelling of each header, never "the last one wins"
            raise Refused("malformed")
        h[str(k).lower()] = v
    try:
        raw_cert = h["orch-channel-cert"]
        if type(raw_cert) is not str or len(raw_cert) > CHANNEL_CERT_HEADER_MAX:
            raise CanonError("cert")
        cert = loads_strict(unb64u(raw_cert))
        if type(h["orch-channel-ts"]) is not str or not TS_RE.fullmatch(h["orch-channel-ts"]):
            raise CanonError("ts")
        ts = int(h["orch-channel-ts"])
        nonce = unhex(h["orch-channel-nonce"], 16)
        sig = unb64u(h["orch-channel-signature"], 64)
        if req["method"] not in CHANNEL_METHODS:
            raise CanonError("method")
        hub, ch, _ = _parse_channel_target(req["target"])
        msg = channel_request_bytes(hub, ch, ts, nonce, req["method"], req["target"], req["body"])
    except KeyError, CanonError, TypeError:
        raise Refused("malformed") from None
    if abs(now_ms - ts) > CHANNEL_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    revoked = frozenset(st.get("revoked_op_certs", []))
    try:
        held = verify_card(st["hub_cards"][hub], now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked)
    except KeyError, Refused:
        raise Refused("unknown_hub") from None
    o = verify_channel_cert(cert, held, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked)
    if o["channel_id"] != ch:
        raise Refused("other_user")
    if not S.verify(unb64u(o["key_pub"], 65), sig, msg):
        raise Refused("bad_channel_signature")
    if ch in set(st.get("revoked_channels", {}).get(hub, [])):
        raise Refused("channel_revoked")
    seen = st.get("nonces", {}).get(f"{hub}/{ch}", {})
    if nonce.hex() in seen and now_ms - seen[nonce.hex()] < CHANNEL_NONCE_RETENTION_MS:
        raise Refused("replay")
    return {"hub": hub, "channel_id": ch, "nonce": nonce.hex()}


def channel_route(method: str, target: str, body: bytes) -> dict[str, Any]:
    """The route of an authenticated channel request (§10.8.4): ``GET .../mailbox`` (the list), ``GET`` and ``DELETE``
    ``.../mailbox/{id}``; a non-empty body is ``malformed``; any other path or method ``not_found``."""
    hub, ch, rest = _parse_channel_target(target)
    out: dict[str, Any] = {"hub": hub, "channel_id": ch}
    if rest == ["mailbox"] and method == "GET":
        route: dict[str, Any] = {"route": "mailbox_list"}
    elif len(rest) == 2 and rest[0] == "mailbox" and _HEX32.fullmatch(rest[1]) and method in CHANNEL_METHODS:
        route = {"route": "mailbox_get" if method == "GET" else "mailbox_delete", "id": rest[1]}
    else:
        raise Refused("not_found")
    if body != b"":
        raise Refused("malformed")
    return {**out, **route}


__all__ = [
    "CHANNEL_SESSION_MS",
    "channel_request_bytes",
    "channel_route",
    "check_channel_request",
    "fingerprint",
    "parse_pair_link",
    "session_ref",
    "verify_api_origin",
    "verify_channel_auth",
    "verify_channel_push",
]
