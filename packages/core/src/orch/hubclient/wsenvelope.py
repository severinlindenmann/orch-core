"""The v2 ws->ws envelope (``contracts/relay/protocol-v2.md`` section 11), end to end: build, and the receiver's
normative order (11.4). It is transport-free: the hub router (this package) and the v2 relay carry the same bytes.

``envelope = header (60) || u32(body_len) || body || signature (64) [|| u32(cosig_len) || cj(cosig)]``
``header   = "ORWX" || 2 || suite || flags || 0 || id (16) || from_ws (16) || to_ws (16) || u32(wxk_version)``
``body     = seal(WXK of to_ws, "ws-envelope", object = id, epoch = wxk_version, cj(b), extra_aad = header)``
``signature = Sign(WSK_from, "orch/v2/ws-envelope|" || header || body)``

The hub never opens, re-signs or edits these bytes; the receiver checks the sender against ITS OWN pin of the sender,
never against anything the transport says. A human co-signature (flag ``COSIGNED``) is not verified by this module
yet, so a co-signed envelope is refused ``bad_cosignature`` (fail closed); nothing here sets the flag.

State lives in :class:`Mailbox` (peers, seen ids, sent envelopes, inbox, delivered replies, unsent outbox). The caller
persists ``Mailbox.to_json()`` after every call that returns an outcome; :func:`receive` mutates the mailbox and the
caller must save BEFORE acknowledging the hub."""

from __future__ import annotations

import os
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from orch.canon.jcs import JcsError, loads_strict
from orch.canon.jcs import dumps as cj
from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u, unhex
from orch.crypto.labels import L
from orch.crypto.sealing import open_sealed, seal

__all__ = [
    "MAGIC",
    "MAX_ENVELOPE",
    "Built",
    "EnvelopeError",
    "Mailbox",
    "Outcome",
    "build_envelope",
    "build_refusal",
    "envelope_sha256",
    "receive",
]

MAGIC = b"ORWX"
HEADER_LEN = 60
MAX_ENVELOPE = 1 << 20
F_COSIGNED, F_REFUSAL = 0x01, 0x02
MAX_DEADLINE_MS = 30 * 24 * 3600 * 1000
SKEW_MS = 300_000
WXK_OVERLAP_MS = 14 * 24 * 3600 * 1000
SEEN_RETENTION_MS = 7 * 24 * 3600 * 1000
MAX_INBOX_PER_PEER = 64
MAX_MAILBOX_BYTES = 8 << 20  # well under the host's 16 MiB read limit
KINDS = ("ticket", "result", "refusal", "question.closed")
FINAL = ("result", "refusal")
REFUSALS = ("stale_wxk", "depth_exceeded", "deadline_passed", "bad_cosignature", "unknown_reply", "malformed")
BODY_KEYS = {"kind", "in_reply_to", "depth", "deadline_ms", "ticket", "result", "refusal", "attachments"}


class EnvelopeError(Exception):
    """Sender side: the envelope cannot be built (``too_large``, ``malformed``, ``unknown_peer``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def envelope_sha256(envelope: bytes) -> str:
    return suite.sha256(envelope).hex()


@dataclass
class Mailbox:
    """What a workspace host keeps for ws->ws. All values are JSON types so the caller can persist it as is.

    * ``peers``: ws hex -> ``{wsk_pub, wxk_pub, wxk_version}`` (b64u): the address book pin (empty: nobody may send).
    * ``seen``: ``"<from>/<id>"`` -> ``{digest, outcome, until}`` (11.4 step 4).
    * ``sent``: id hex -> ``{to, consumed: [kinds], deadline_ms, sha256, state, result}``.
    * ``inbox``: ``"<from_ws>/<id>"`` -> ``{envelope_id, from_ws, sha256, depth, deadline_ms, ticket, attachments,
      state}`` (``new`` | ``accepted``
      | ``replied``); never overwritten.
    * ``outbox``: id hex -> b64u envelope bytes the hub did not take yet (resent unchanged: idempotent by id)."""

    peers: dict[str, dict[str, Any]] = field(default_factory=dict)
    seen: dict[str, dict[str, Any]] = field(default_factory=dict)
    sent: dict[str, dict[str, Any]] = field(default_factory=dict)
    inbox: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbox: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"peers": self.peers, "seen": self.seen, "sent": self.sent, "inbox": self.inbox, "outbox": self.outbox}

    def size(self) -> int:
        """The bytes the saved mailbox takes (the cap in :data:`MAX_MAILBOX_BYTES` is on this)."""
        return len(cj(self.to_json()))

    @classmethod
    def from_json(cls, d: Any) -> Mailbox:
        keys = {"peers", "seen", "sent", "inbox", "outbox"}
        if type(d) is not dict or set(d) != keys or any(type(d[k]) is not dict for k in keys):
            raise EnvelopeError("malformed")
        return cls(**{k: dict(d[k]) for k in sorted(keys)})

    def pin_peer(
        self,
        workspace_id: str,
        wsk_pub: bytes,
        wxk_pub: bytes,
        wxk_version: int,
        *,
        owner_pk: bytes = b"",
        card_seq: int = 0,
        replace: bool = False,
    ) -> str:
        """Pin a peer. A pin is never replaced silently: another ``WSK`` or owner key, an older card, or the same
        card sequence with other keys is ``pin_conflict`` unless ``replace`` (the owner's explicit, audited choice). The
        same owner key with a newer ``card_seq`` may rotate the ``WXK``. Returns ``new`` | ``same`` | ``updated`` |
        ``replaced``."""
        unhex(workspace_id, 16)
        new = {
            "wsk_pub": b64u(wsk_pub), "wxk_pub": b64u(wxk_pub), "wxk_version": wxk_version,
            "owner_pk": b64u(owner_pk), "card_seq": card_seq,
        }  # fmt: skip
        old = self.peers.get(workspace_id)
        if old is None:
            self.peers[workspace_id] = new
            return "new"
        if replace:
            self.peers[workspace_id] = new
            return "replaced"
        same_keys = old["wsk_pub"] == new["wsk_pub"] and old.get("owner_pk", "") == new["owner_pk"]
        if not same_keys:
            raise EnvelopeError("pin_conflict")
        seq = old.get("card_seq", 0)
        if card_seq < seq or (card_seq == seq and old != new):
            raise EnvelopeError("pin_conflict")
        if old == new:
            return "same"
        self.peers[workspace_id] = new
        return "updated"


@dataclass(frozen=True)
class Built:
    envelope: bytes
    envelope_id: str
    sha256: str


@dataclass(frozen=True)
class Outcome:
    """What :func:`receive` decided. ``action``: ``inbox`` (a ticket is stored), ``deliver`` (a reply is stored),
    ``duplicate`` (nothing new), ``refuse`` (``reply`` is a sealed refusal envelope to send back) or ``drop`` (never
    answered). Drops and refusals are acknowledged to the hub with an error code so the entry leaves its queue."""

    action: str
    code: str | None = None
    item: dict[str, Any] | None = None
    reply: bytes | None = None


def _result_ok(r: Any) -> bool:
    """A peer's ``result`` is data we later render: a dict whose ``summary``, if present, is a string."""
    return type(r) is dict and ("summary" not in r or type(r["summary"]) is str)


def _header(flags: int, env_id: bytes, from_ws: bytes, to_ws: bytes, wxk_version: int) -> bytes:
    return struct.pack(">4sBBBB16s16s16sI", MAGIC, 2, suite.SUITE_ID, flags, 0, env_id, from_ws, to_ws, wxk_version)


def build_envelope(
    *,
    mailbox: Mailbox,
    sign: Callable[[bytes], bytes],
    from_ws: str,
    to_ws: str,
    body: dict[str, Any],
    flags: int = 0,
    env_id: bytes | None = None,
) -> Built:
    """Seal ``body`` to the pinned peer's WXK and sign. ``body`` must have the exact field set of 11.2. Refuses
    ``unknown_peer`` (not in the address book), ``malformed``, ``too_large`` (over 1 MiB) BEFORE anything is sent."""
    peer = mailbox.peers.get(to_ws)
    if peer is None:
        raise EnvelopeError("unknown_peer")
    if set(body) != BODY_KEYS or body["kind"] not in KINDS:
        raise EnvelopeError("malformed")
    try:
        env_id = env_id or os.urandom(16)
        v = peer["wxk_version"]
        hb = _header(flags, env_id, unhex(from_ws, 16), unhex(to_ws, 16), v)
        sealed = seal(unb64u(peer["wxk_pub"], 65), unhex(to_ws, 16), "ws-envelope", env_id, v, cj(body), hb)
    except (EncodingError, JcsError, suite.CryptoError, struct.error):
        raise EnvelopeError("malformed") from None
    sig = sign(L["sig_ws_envelope"] + hb + sealed)
    env = hb + struct.pack(">I", len(sealed)) + sealed + sig
    if len(env) > MAX_ENVELOPE:
        raise EnvelopeError("too_large")
    return Built(env, env_id.hex(), envelope_sha256(env))


def _body(kind: str, **kw: Any) -> dict[str, Any]:
    b = {"kind": kind, "in_reply_to": None, "depth": 0, "deadline_ms": 0, "ticket": None, "result": None,
         "refusal": None, "attachments": []}  # fmt: skip
    b.update(kw)
    return b


def build_refusal(
    *, mailbox: Mailbox, sign: Callable[[bytes], bytes], me: str, sender: str, refused_id: str, code: str,
    now_ms: int, wxk_version: int | None = None,
) -> Built:  # fmt: skip
    """11.5: a sealed envelope to the sender with the signed REFUSAL flag. Never built for a REFUSAL envelope."""
    r: dict[str, Any] = {"code": code}
    if wxk_version is not None:
        r["wxk_version"] = wxk_version
    body = _body("refusal", in_reply_to=refused_id, deadline_ms=now_ms, refusal=r)
    return build_envelope(mailbox=mailbox, sign=sign, from_ws=me, to_ws=sender, body=body, flags=F_REFUSAL)


def new_ticket_body(ticket: dict[str, Any], *, deadline_ms: int, depth: int = 0) -> dict[str, Any]:
    return _body("ticket", depth=depth, deadline_ms=deadline_ms, ticket=ticket)


def new_result_body(in_reply_to: str, result: dict[str, Any], *, deadline_ms: int) -> dict[str, Any]:
    return _body("result", in_reply_to=in_reply_to, deadline_ms=deadline_ms, result=result)


def split(env: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    if not HEADER_LEN + 4 + 16 + suite.SIG_LEN <= len(env) <= MAX_ENVELOPE:
        raise ValueError("size")
    n = struct.unpack(">I", env[HEADER_LEN : HEADER_LEN + 4])[0]
    p = HEADER_LEN + 4
    if p + n + suite.SIG_LEN > len(env):
        raise ValueError("body length")
    return env[:HEADER_LEN], env[p : p + n], env[p + n : p + n + suite.SIG_LEN], env[p + n + suite.SIG_LEN :]


def receive(
    env: bytes,
    *,
    mailbox: Mailbox,
    me: str,
    wxk_for: Callable[[int], tuple[EllipticCurvePrivateKey, int | None] | None],
    wxk_current: int,
    sign: Callable[[bytes], bytes],
    now_ms: int,
) -> Outcome:
    """11.4 in its normative order. ``wxk_for(version)`` returns ``(private key, retired_ms or None)`` or ``None``.
    Mutates ``mailbox``; the caller saves it before acknowledging the hub."""
    try:
        hb, body, sig, rest = split(env)
    except ValueError:
        return Outcome("drop", "size")
    magic, v, s, flags, res, env_id, from_b, to_b, wxk_version = struct.unpack(">4sBBBB16s16s16sI", hb)
    if magic != MAGIC or v != 2 or res != 0 or flags & ~(F_COSIGNED | F_REFUSAL):
        return Outcome("drop", "shape")
    if s != suite.SUITE_ID:
        return Outcome("drop", "suite")
    from_ws, to_ws = from_b.hex(), to_b.hex()
    if to_ws != me:
        return Outcome("drop", "not_for_us")
    peer = mailbox.peers.get(from_ws)  # the pin: an unpinned sender is never answered
    if peer is None:
        return Outcome("drop", "not_pinned")
    try:
        ok = suite.verify(unb64u(peer["wsk_pub"], 65), sig, L["sig_ws_envelope"] + hb + body)
    except (EncodingError, KeyError, TypeError, suite.CryptoError):
        ok = False
    if not ok:
        return Outcome("drop", "signature")
    is_refusal = bool(flags & F_REFUSAL)
    digest = suite.sha256(hb + body).hex()
    seen_key = f"{from_ws}/{env_id.hex()}"
    seen = mailbox.seen.get(seen_key)
    if seen is not None and now_ms < seen["until"]:
        if seen["digest"] != digest:
            return Outcome("drop", "id_conflict")
        return Outcome("duplicate", item=seen["outcome"])

    def done(o: Outcome, deadline: int = 0) -> Outcome:
        if is_refusal and o.action == "refuse":  # 11.5: a refusal is never answered
            o = Outcome("drop", o.code)
        mailbox.seen[seen_key] = {
            "digest": digest,
            "outcome": {"action": o.action, "code": o.code},
            "until": max(now_ms, deadline) + SEEN_RETENTION_MS,
        }
        return o

    def refuse(code: str, **extra: int) -> Outcome:
        reply = None
        if not is_refusal:
            try:
                reply = build_refusal(mailbox=mailbox, sign=sign, me=me, sender=from_ws, refused_id=env_id.hex(),
                                      code=code, now_ms=now_ms, **extra).envelope  # fmt: skip
            except EnvelopeError:
                reply = None  # the pin has no usable WXK: nothing to seal to, the refusal is still recorded
        return done(Outcome("refuse", code, reply=reply))

    if flags & F_COSIGNED:
        return refuse("bad_cosignature")  # not verified here yet: fail closed
    if rest:
        return Outcome("drop", "trailing_bytes")
    key = wxk_for(wxk_version)
    if key is None or (key[1] is not None and now_ms >= key[1] + WXK_OVERLAP_MS):
        return refuse("stale_wxk", wxk_version=wxk_current)
    try:
        p = loads_strict(open_sealed(key[0], to_b, "ws-envelope", env_id, wxk_version, body, hb))
        if type(p) is not dict or set(p) != BODY_KEYS or p["kind"] not in KINDS:
            raise ValueError("body")
        if type(p["depth"]) is not int or type(p["deadline_ms"]) is not int or type(p["attachments"]) is not list:
            raise ValueError("body")
        if (p["kind"] == "refusal") != is_refusal:
            raise ValueError("refusal flag")
        if is_refusal and (type(p["refusal"]) is not dict or p["refusal"].get("code") not in REFUSALS):
            raise ValueError("refusal body")
    except (InvalidTag, ValueError, JcsError, UnicodeDecodeError, suite.CryptoError):
        return refuse("malformed")
    sha = envelope_sha256(env)
    if p["kind"] == "result" and not _result_ok(p["result"]):
        return refuse("malformed")
    if p["kind"] == "ticket":
        if p["ticket"] is None or type(p["ticket"]) is not dict or p["depth"] < 0 or p["in_reply_to"] is not None:
            return refuse("malformed")
        if p["depth"] > 1:
            return refuse("depth_exceeded")
        if p["deadline_ms"] > now_ms + MAX_DEADLINE_MS:
            return refuse("malformed")
        if p["deadline_ms"] + SKEW_MS < now_ms:
            return refuse("deadline_passed")
        mine = sum(1 for v in mailbox.inbox.values() if v["from_ws"] == from_ws)
        if mine >= MAX_INBOX_PER_PEER or mailbox.size() + len(env) > MAX_MAILBOX_BYTES:
            return Outcome("drop", "mailbox_full")  # acked with an error: the hub deletes it; not recorded as seen
        item = {
            "envelope_id": env_id.hex(),
            "from_ws": from_ws,
            "sha256": sha,
            "depth": p["depth"],
            "deadline_ms": p["deadline_ms"],
            "ticket": p["ticket"],
            "attachments": p["attachments"],
            "state": "new",
        }
        mailbox.inbox.setdefault(seen_key, item)  # keyed per sender: another sender's id never collides
        return done(Outcome("inbox", item=item), p["deadline_ms"])
    # a reply must answer an envelope we sent to this peer; each kind is taken once, result and refusal together
    sent = mailbox.sent.get(p["in_reply_to"]) if type(p["in_reply_to"]) is str else None
    group = FINAL if p["kind"] in FINAL else (p["kind"],)
    if sent is None or sent["to"] != from_ws or any(k in sent["consumed"] for k in group):
        return refuse("unknown_reply")
    sent["consumed"].append(p["kind"])
    late = p["kind"] == "result" and p["deadline_ms"] + SKEW_MS < now_ms
    item = {"kind": p["kind"], "from_ws": from_ws, "late": late, "result": p["result"], "sha256": sha}
    if is_refusal:
        item["code"] = p["refusal"]["code"]
    sent["state"] = "answered" if p["kind"] in FINAL else sent["state"]
    sent["reply"] = item
    return done(Outcome("deliver", item=item), p["deadline_ms"])
