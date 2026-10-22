"""Phase-4 contract extension (protocol.md §6.3, §8.4–§8.5, §10.4 proposals, §10.7 log_sync, §10.8 relay requests;
V3-D23). Pure reference functions, no I/O, no clock, no key storage.

* :func:`check_action` - what a channel may ask the hub to build (an action, never event bytes);
* :func:`seal_view_page` / :func:`apply_view_page` - the hub's ``WK_e``-sealed view of a workspace log, verified line by
  line under the pinned ``WSK`` (host_sig and the event chain) before the hub builds anything from it;
* :func:`verify_channel_register` - proof of possession of a new channel key, under its own label;
* :func:`verify_space_proposal` / :func:`accept_from_proposal` - the HK_op-signed transition an invited workspace checks
  before its owner signs ``space.accept``;
* :func:`relay_request_bytes` / :func:`check_relay_request` - HK_op-signed hub -> relay requests (E1).
"""

from __future__ import annotations

import re
import struct
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import spaces as SP
from . import suite as S
from .canon import CanonError, b64u, cj, loads_strict, unb64u, unhex
from .hub import (
    _TS as TS_RE,
)
from .hub import (
    CLOCK_SKEW_MS,
    CUSTODIAL_FIELDS,
    NOT_SIGNED,
    Refused,
    _dev,
    _envelope,
    _fields,
    _hex16,
    _key_field,
    _lp16,
    _signature,
    _under_card,
    check_custodial_role,
    log_kind,
    verify_card,
)
from .labels import KDF_HUB_VIEW, KDF_HUB_VIEW_MSG, L

# --- §8.5 actions: what a channel may ask for -------------------------------------------------------------------------

_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
GATES = ("requirements", "plan", "code")


def _str(n: int) -> Any:
    return lambda x: type(x) is str and 0 < len(x.encode()) <= n


def _hash(x: Any) -> bool:
    return type(x) is str and _HASH.fullmatch(x) is not None


def _enum(*v: str) -> Any:
    return lambda x: type(x) is str and x in v


def _obj(x: Any) -> bool:
    return type(x) is dict and len(x) > 0


def _ids(x: Any) -> bool:
    return type(x) is list and all(type(i) is str and 0 < len(i) <= 64 for i in x)


#: type -> (required fields, optional fields). ``type`` and ``log`` are always present (``ticket.created`` has no
#: ``log``: the hub makes the new ticket's uid). Decisions carry the ``hash`` the person reviewed: the hub refuses with
#: ``event_changed`` when its view no longer has that hash. Every other envelope or custodial field is the hub's.
ACTIONS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "ticket.created": ({"ticket_type": _str(64), "title": _str(500)}, {}),
    "ticket.updated": ({"set": _obj}, {}),
    "status.changed": ({"to": _enum("backlog", "open")}, {"reason": _str(500)}),
    "ticket.submitted": ({}, {}),
    "ticket.closed": (
        {"resolution": _enum("wont_do", "duplicate", "obsolete", "other")},
        {"duplicate_of": _str(64), "text": _str(4096)},
    ),
    "ticket.reopened": ({}, {"text": _str(4096)}),
    "ticket.moved": ({"to": _enum("backlog", "open"), "reason": _str(500)}, {}),
    "visibility.changed": ({"visibility": _str(32)}, {}),
    "people.changed": (
        {"role": _enum("owner", "assignees", "reviewers", "watchers"), "add": _ids, "remove": _ids},
        {},
    ),
    "policy.changed": ({"gates": _obj}, {}),
    "claim.released": ({"session": _str(64), "reason": _str(64)}, {}),
    "log.added": ({"text": _str(4096)}, {}),
    "handoff.written": ({"text": _str(2048)}, {}),
    "question.asked": ({"question": _obj}, {}),
    "question.answered": ({"question": _str(64), "hash": _hash}, {"option": _str(64), "text": _str(4096)}),
    "gate.approved": ({"gate": _enum(*GATES), "hash": _hash}, {}),
    "gate.changes_requested": ({"gate": _enum(*GATES), "hash": _hash, "text": _str(4096)}, {}),
    "verdict.given": ({"outcome": _enum("pass", "fail"), "hash": _hash}, {"text": _str(4096)}),
    "settings.changed": ({"set": _obj}, {}),
    "addon.disabled": ({"name": _str(64)}, {}),
    "addon.purged": ({"name": _str(64)}, {}),
}
#: Names an action can never carry: the envelope, the custodial fields and the host fields are built by the hub/host.
RESERVED = frozenset(
    {
        "v",
        "id",
        "actor",
        "based_on",
        "roster_v",
        "hash_v",
        "gate_gen",
        "policy_hash",
        "source_sha",
        "qid",
        "base_rev",
        "sections",
        *CUSTODIAL_FIELDS,
        *NOT_SIGNED,
    }
)


def check_action(action: Any, *, role: Any) -> dict[str, Any]:
    """The channel's request to the hub (``prepare``), checked before the hub reads its view: exact fields per type,
    the type custodial for its log, and the person's hub role high enough. Returns the action."""
    if type(action) is not dict or type(action.get("type")) is not str:
        raise Refused("malformed")
    typ = action["type"]
    if any(k in RESERVED for k in action if k not in ("type", "log")):
        raise Refused("malformed")
    if typ == "ticket.created":
        log = "new"
        base = {"type"}
    else:
        log = action.get("log")
        if type(log) is not str or not (log == "workspace" or _ULID.fullmatch(log)):
            raise Refused("malformed")
        base = {"type", "log"}
    if typ not in ACTIONS:
        raise Refused("not_custodial")
    check_custodial_role(typ, "ticket" if log == "new" else log, role)
    req, opt = ACTIONS[typ]
    keys = set(action) - base
    if not set(req) <= keys or not keys <= set(req) | set(opt):
        raise Refused("malformed")
    for k in keys:
        if not {**req, **opt}[k](action[k]):
            raise Refused("malformed")
    if typ == "question.answered" and not ({"option", "text"} & keys):
        raise Refused("malformed")
    if typ == "verdict.given" and action["outcome"] == "fail" and "text" not in keys:
        raise Refused("malformed")
    if typ == "ticket.closed" and ("duplicate_of" in keys) != (action["resolution"] == "duplicate"):
        raise Refused("malformed")
    return action


def check_prepared(ctx: Any, digest_hex: Any) -> bytes:
    """What a channel does with the hub's ``prepare`` reply before it shows anything (WYSIWYS, §8.4): the digest it
    will sign is recomputed from the context it renders, never taken from the hub."""
    from .hub import event_digest

    if (
        type(ctx) is not dict
        or set(ctx) != {"contract", "suite", "workspace_id", "log", "event"}
        or ctx["contract"] != 1
        or ctx["suite"] != S.SUITE_ID
        or type(ctx["event"]) is not dict
        or "channel_proof" in ctx["event"]
        or ctx["event"].get("auth") != "hub"
    ):
        raise Refused("malformed")
    d = event_digest(ctx["workspace_id"], ctx["log"], ctx["event"])
    if type(digest_hex) is not str or d.hex() != digest_hex:
        raise Refused("event_changed")
    return d


# --- §10.7 log_sync: the hub's view of a workspace log ---------------------------------------------------------------

VIEW_MAX_LINES = 256
VIEW_MAX_SEALED = 1 << 20


def _u32(n: int) -> bytes:
    return struct.pack(">I", n)


def view_key(wk_e: bytes, workspace_id: str, epoch: int) -> bytes:
    """``HKDF(WK_e, salt = "", info = "orch/v2/hub-view|" hex(ws) "|" dec(epoch))``, like v2 §8.2's ``K_bridge``."""
    info = KDF_HUB_VIEW + b"|" + workspace_id.encode() + b"|" + str(epoch).encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(wk_e)


def view_aad(workspace_id: str, epoch: int, log: str, from_seq: int, count: int, head: str, reset: bool) -> bytes:
    if not _HASH.fullmatch(head):
        raise Refused("malformed")
    return (
        L["aad_hub_view"]
        + bytes([S.SUITE_ID])
        + unhex(workspace_id, 16)
        + _u32(epoch)
        + _lp16(log.encode("ascii"))
        + struct.pack(">Q", from_seq)
        + _u32(count)
        + bytes.fromhex(head[7:])
        + bytes([1 if reset else 0])
    )


def event_head(event: Mapping[str, Any]) -> str:
    """ticket-format §5.5: ``"sha256:" + hex(H("orch/v2/event|" || cj(event)))`` over the full event."""
    try:
        return "sha256:" + S.sha256(L["h_event"] + cj(dict(event))).hex()
    except CanonError:
        raise Refused("malformed") from None


def host_sig_bytes(workspace_id: str, log: str, event: Mapping[str, Any]) -> bytes:
    """ticket-format §5.5: ``"orch/v2/sig/host-event|" || cj(ctx)`` over the event without ``host_sig``."""
    e = {k: v for k, v in event.items() if k != "host_sig"}
    try:
        return L["sig_host_event"] + cj(
            {"contract": 1, "suite": S.SUITE_ID, "workspace_id": workspace_id, "log": log, "event": e}
        )
    except CanonError:
        raise Refused("malformed") from None


def _log_ok(log: Any) -> bool:
    return type(log) is str and (log == "workspace" or _ULID.fullmatch(log) is not None)


def seal_view_page(
    wk_e: bytes,
    workspace_id: str,
    epoch: int,
    log: str,
    from_seq: int,
    lines: list[dict[str, Any]],
    salt: bytes,
    *,
    reset: bool = False,
) -> dict[str, Any]:
    """The host's ``log_sync`` body: the exact log lines ``from_seq ..`` sealed with SALTED-AEAD (v2 §5.1) under the
    view key of ``epoch``. Test/host helper; ``salt`` is 16 fresh bytes in real use."""
    head = event_head(lines[-1])
    aad = view_aad(workspace_id, epoch, log, from_seq, len(lines), head, reset)
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=KDF_HUB_VIEW_MSG).derive(
        view_key(wk_e, workspace_id, epoch)
    )
    sealed = salt + AESGCM(k).encrypt(bytes(12), cj({"lines": lines}), aad)
    return {
        "epoch": epoch,
        "log": log,
        "from_seq": from_seq,
        "count": len(lines),
        "head": head,
        "reset": reset,
        "sealed": b64u(sealed),
    }


def open_view_page(wk_e: bytes, workspace_id: str, body: Mapping[str, Any]) -> list[Any]:
    try:
        raw = unb64u(body["sealed"])
    except CanonError:
        raise Refused("malformed") from None
    if len(raw) > VIEW_MAX_SEALED:
        raise Refused("too_large")
    if len(raw) < 16 + 16:
        raise Refused("bad_wrap")
    aad = view_aad(
        workspace_id, body["epoch"], body["log"], body["from_seq"], body["count"], body["head"], body["reset"]
    )
    k = HKDF(algorithm=hashes.SHA256(), length=32, salt=raw[:16], info=KDF_HUB_VIEW_MSG).derive(
        view_key(wk_e, workspace_id, body["epoch"])
    )
    try:
        pt = AESGCM(k).decrypt(bytes(12), raw[16:], aad)
    except InvalidTag:
        raise Refused("bad_wrap") from None
    try:
        doc = loads_strict(pt)
    except CanonError:
        raise Refused("malformed") from None
    if type(doc) is not dict or set(doc) != {"lines"} or type(doc["lines"]) is not list:
        raise Refused("malformed")
    return doc["lines"]


def apply_view_page(
    views: Mapping[str, list[str]],
    body: Any,
    *,
    workspace_id: str,
    wsk_pub: bytes,
    wk_epochs: Mapping[int, bytes],
) -> dict[str, list[str]]:
    """The hub's check of one ``log_sync`` body, in order (§10.7). ``views`` maps each log to the event heads it holds
    (index ``i`` = seq ``i + 1``); ``wk_epochs`` the ``WK_e`` the hub can open (from its sealed wraps). Returns the new
    ``views``. Nothing is built from a page that did not pass every step."""
    if type(body) is not dict or set(body) != {"epoch", "log", "from_seq", "count", "head", "reset", "sealed"}:
        raise Refused("malformed")
    if (
        type(body["epoch"]) is not int
        or not _log_ok(body["log"])
        or type(body["from_seq"]) is not int
        or body["from_seq"] < 1
        or type(body["count"]) is not int
        or not 1 <= body["count"] <= VIEW_MAX_LINES
        or type(body["head"]) is not str
        or not _HASH.fullmatch(body["head"])
        or type(body["reset"]) is not bool
        or (body["reset"] and body["from_seq"] != 1)
    ):
        raise Refused("malformed")
    # 1. the epoch: only the newest WK_e the hub holds (it seals after wk_seal, never before)
    if not wk_epochs or body["epoch"] != max(wk_epochs):
        raise Refused("bad_epoch")
    log, start = body["log"], body["from_seq"]
    held = [] if body["reset"] else list(views.get(log, []))
    # 2. position: a page extends the held view or overlaps it; a hole is a gap
    if start > len(held) + 1:
        raise Refused("view_gap")
    # 3. open (AEAD under the view key; the AAD binds workspace, epoch, log, range, head, reset)
    lines = open_view_page(wk_epochs[body["epoch"]], workspace_id, body)
    if len(lines) != body["count"]:
        raise Refused("malformed")
    ws_view = len(views.get("workspace", [])) if log != "workspace" else None
    prev_head = held[start - 2] if start > 1 else None
    out = held[: start - 1]
    for i, e in enumerate(lines):
        seq = start + i
        if type(e) is not dict or e.get("seq") != seq or "host_sig" not in e:
            raise Refused("malformed")
        # 4. every line is the host's: host_sig under the pinned WSK (ticket-format §5.5)
        try:
            hs = unb64u(e["host_sig"], S.SIG_LEN)
        except CanonError:
            raise Refused("malformed") from None
        if not S.verify(wsk_pub, hs, host_sig_bytes(workspace_id, log, e)):
            raise Refused("bad_signature")
        # 5. the chain: prev is the head of the line before (null at seq 1)
        if e.get("prev") != prev_head:
            raise Refused("view_fork")
        h = event_head(e)
        if seq <= len(held) and held[seq - 1] != h:
            raise Refused("view_fork")
        # 6. a ticket line never runs ahead of the workspace log the hub holds (sync the workspace log first)
        if ws_view is not None and (type(e.get("ws_seq")) is not int or e["ws_seq"] > ws_view):
            raise Refused("view_gap")
        out.append(h)
        prev_head = h
    if prev_head != body["head"]:
        raise Refused("malformed")
    if len(held) > len(out):  # an overlap that matched keeps the longer held view
        out = held
    return {**views, log: out}


# --- §6.3 channel registration ---------------------------------------------------------------------------------------

#: the factor a channel certificate gets follows the gate the hub verified, never the caller's choice (§6.3)
FACTOR_OF = {("phone", "pairing-code"): "se-unlocked", ("browser", "session"): "password+totp"}


def verify_channel_register(
    signed: Any, *, hub_id: str, challenge: Mapping[str, Any], now_ms: int, dev_mode: bool
) -> dict[str, Any]:
    """The hub's check of a registration proof. ``challenge`` is what the hub stored when it opened the registration:
    ``{user_id, nonce, expires_ms, gate}`` with ``gate`` the check it already passed (``pairing-code`` or
    ``session``). Returns ``{channel_id, user_id, key_pub, factor}``; the factor comes from the gate."""
    o = _envelope(signed, "hub.channel_register")
    key = _key_field(o, "key_pub")
    _signature(signed, L["sig_hub_channel_register"], key)
    _fields(o, "hub.channel_register")
    if o["hub_id"] != hub_id:
        raise Refused("other_hub")
    if o["user_id"] != challenge["user_id"]:
        raise Refused("other_user")
    if o["nonce"] != challenge["nonce"] or o["expires_ms"] != challenge["expires_ms"] or now_ms >= o["expires_ms"]:
        raise Refused("challenge_invalid")
    factor = FACTOR_OF.get((o["channel_kind"], o["attestation"]))
    if factor is None:
        raise Refused("malformed")
    if o["attestation"] != challenge["gate"]:
        raise Refused("factor_mismatch")
    _dev(o, dev_mode)
    from .hub import channel_id

    return {"channel_id": channel_id(key), "user_id": o["user_id"], "key_pub": o["key_pub"], "factor": factor}


# --- §10.4 the hub-signed space proposal (#1020) ----------------------------------------------------------------------


def _member_list(x: list[Any], lo: int) -> None:
    if not lo <= len(x) <= SP.MAX_MEMBERS or any(not _hex16(w) for w in x) or x != sorted(set(x)):
        raise Refused("malformed")


def verify_space_proposal(
    signed: Any,
    held: Mapping[str, Any],
    *,
    workspace_id: str,
    now_ms: int,
    dev_mode: bool,
    held_space: Mapping[str, Any] | None = None,
    revoked_op_certs: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """The invited host's check of a ``hub.space_proposal`` before its owner is asked (§10.4). ``held_space`` is the
    ``hub.space`` it already holds for that space, if it was ever a member."""
    o = _under_card(signed, "hub.space_proposal", held, dev_mode, revoked_op_certs)
    _member_list(o["prev_members"], 0)
    _member_list(o["members"], 1)
    if (o["epoch"] == 1) != (o["prev_members"] == []):
        raise Refused("malformed")
    if o["workspace_id"] not in o["members"] or o["workspace_id"] in o["prev_members"]:
        raise Refused("malformed")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if o["expires_ms"] <= o["issued_ms"] or o["issued_ms"] > now_ms + CLOCK_SKEW_MS:
        raise Refused("cert_invalid")
    if now_ms >= o["expires_ms"]:
        raise Refused("cert_expired")
    if held_space is not None and (held_space["space_id"] != o["space_id"] or o["epoch"] <= held_space["epoch"]):
        raise Refused("stale_epoch")
    return o


def accept_from_proposal(p: Mapping[str, Any]) -> dict[str, Any]:
    """The ``space.accept`` object (unsigned) the host builds from a verified proposal, after its owner confirmed: the
    hashes come from the signed lists, never from a hub admin's text."""
    return {
        "v": 2,
        "suite": S.SUITE_ID,
        "kind": "space.accept",
        "hv": 0,
        "hub_id": p["hub_id"],
        "space_id": p["space_id"],
        "workspace_id": p["workspace_id"],
        "epoch": p["epoch"],
        "prev_members_hash": SP.members_hash(p["prev_members"]),
        "members_hash": SP.members_hash(p["members"]),
        "dev": p["dev"],
    }


# --- §10.8 hub -> relay requests (E1) --------------------------------------------------------------------------------

RELAY_WINDOW_MS = 120_000
RELAY_NONCE_RETENTION_MS = 240_000
RELAY_METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")
RELAY_TARGET_MAX = 2048
_SEG = re.compile(r"[A-Za-z0-9._~-]+")
_QPART = re.compile(r"[A-Za-z0-9._~-]+")


def parse_relay_target(target: Any, hub_id: str) -> None:
    """Canonical target (§10.8): ASCII, at most 2048 bytes, path ``/v2/hubs/<hub_id>`` or below it with unreserved
    characters only (no ``%``, ``+``, ``#``, ``;``, empty, ``.`` or ``..`` segments, no trailing ``/``), and an
    optional query of unique ``key=value`` pairs of unreserved characters. The ``hub_id`` segment must be the signing
    hub's (``other_hub``)."""
    if type(target) is not str or not target.isascii() or len(target) > RELAY_TARGET_MAX:
        raise Refused("malformed")
    path, sep, query = target.partition("?")
    segs = path.split("/")
    if segs[0] != "" or len(segs) < 4 or segs[1:3] != ["v2", "hubs"]:
        raise Refused("malformed")
    for s in segs[1:]:
        if not _SEG.fullmatch(s) or s in (".", ".."):
            raise Refused("malformed")
    if not re.fullmatch(r"[0-9a-f]{32}", segs[3]):
        raise Refused("malformed")
    if sep:
        keys = []
        for part in query.split("&"):
            k, eq, v = part.partition("=")
            if not eq or not _QPART.fullmatch(k) or not _QPART.fullmatch(v):
                raise Refused("malformed")
            keys.append(k)
        if len(keys) != len(set(keys)):
            raise Refused("malformed")
    if segs[3] != hub_id:
        raise Refused("other_hub")


def relay_request_bytes(
    hub_id: str, op_cert_id: str, ts_ms: int, nonce: bytes, method: str, target: str, body: bytes
) -> bytes:
    """``"orch/v2/hub-relay-req|" || suite || hub_id (16) || op_cert_id (16) || u64(ts_ms) || nonce (16) ||
    lp16(METHOD) || lp16(target) || H(body)``."""
    if type(ts_ms) is not int or not 0 <= ts_ms < 1 << 64 or type(nonce) is not bytes or len(nonce) != 16:
        raise Refused("malformed")
    for part in (method, target):
        if type(part) is not str or not part.isascii():
            raise Refused("malformed")
    if type(body) is not bytes:
        raise Refused("malformed")
    return (
        L["sig_hub_relay_request"]
        + bytes([S.SUITE_ID])
        + unhex(hub_id, 16)
        + unhex(op_cert_id, 16)
        + struct.pack(">Q", ts_ms)
        + nonce
        + _lp16(method.encode("ascii"))
        + _lp16(target.encode("ascii"))
        + S.sha256(body)
    )


def check_relay_request(
    req: Mapping[str, Any], st: Mapping[str, Any], *, now_ms: int, dev_mode: bool
) -> dict[str, Any]:
    """The relay's check of a hub request (§10.8), in order. ``req`` = {method, target, headers, body (bytes)}; ``st`` =
    {hub_cards: {hub_id: signed card}, revoked_op_certs: [..], nonces: {hub_id: {nonce hex: ts_ms}}}. Returns the hub
    and the nonce to record (the caller stores it for :data:`RELAY_NONCE_RETENTION_MS`)."""
    h = {str(k).lower(): v for k, v in req["headers"].items()}
    try:
        hub = h["orch-hub"]
        unhex(hub, 16)
        op_id = h["orch-hub-op-cert"]
        unhex(op_id, 16)
        if type(h["orch-hub-ts"]) is not str or not TS_RE.fullmatch(h["orch-hub-ts"]):
            raise CanonError("ts")
        ts = int(h["orch-hub-ts"])
        nonce = unhex(h["orch-hub-nonce"], 16)
        sig = unb64u(h["orch-hub-signature"], 64)
        if req["method"] not in RELAY_METHODS:
            raise CanonError("method")
        parse_relay_target(req["target"], hub)
        msg = relay_request_bytes(hub, op_id, ts, nonce, req["method"], req["target"], req["body"])
    except KeyError, CanonError:
        raise Refused("malformed") from None
    except Refused as r:
        raise Refused("other_hub" if r.code == "other_hub" else "malformed") from None
    if abs(now_ms - ts) > RELAY_WINDOW_MS:
        raise Refused("stale_timestamp", server_ms=now_ms)
    try:  # the held card, verified again for this request (op cert valid, not revoked, root's hub_id, dev marker)
        held = verify_card(
            st["hub_cards"][hub],
            now_ms=now_ms,
            dev_mode=dev_mode,
            revoked_op_certs=frozenset(st.get("revoked_op_certs", [])),
        )
    except KeyError, Refused:
        raise Refused("unknown_hub") from None
    if op_id != held["op_cert"]["cert_id"]:
        raise Refused("stale_op_cert")
    if not S.verify(held["hk_op_pub"], sig, msg):
        raise Refused("bad_hub_signature")
    seen = st.get("nonces", {}).get(hub, {})
    if nonce.hex() in seen and now_ms - seen[nonce.hex()] < RELAY_NONCE_RETENTION_MS:
        raise Refused("replay")
    return {"hub": hub, "nonce": nonce.hex()}


__all__ = [
    "ACTIONS",
    "FACTOR_OF",
    "accept_from_proposal",
    "apply_view_page",
    "check_action",
    "check_prepared",
    "check_relay_request",
    "event_head",
    "host_sig_bytes",
    "log_kind",
    "open_view_page",
    "parse_relay_target",
    "relay_request_bytes",
    "seal_view_page",
    "verify_channel_register",
    "verify_space_proposal",
    "view_aad",
    "view_key",
]
