"""Remote access through the hub, contract R0 (protocol.md §10.2c, §8.5 "No custodial type can"; spec
``orch-v2-hub-remote.md`` §5, §7, D84-D93).

This is the reference the vectors (``contracts/hub/vectors_remote.json``) are built from and replayed against on the
hub side. It is pure: no I/O, no clock, no state. R2 (host policy, devices, enrollment), R3 (host session) and R4 (hub
stream router) build the real paths and replay the same vectors; orch-core replays them with its own test-side port
(``packages/core/tests/host/remote_port.py``), so the two sides do not share code.

What is here:

- the three link stream ops (``stream_open``, ``stream_data``, ``stream_close``), their exact bodies and the receiver's
  checks (:func:`verify_stream_link`), the frame cost (:func:`frame_cost`);
- ``Noise_IK_25519_ChaChaPoly_SHA256`` (Noise rev 34) built only from ``cryptography`` primitives (X25519,
  ChaCha20-Poly1305, HMAC-SHA256), with the orch prologue, an empty message-1 payload and a 32-byte challenge as the
  message-2 payload;
- the presence key's two signing domains (``remote-open``, ``remote-presence``) and both assertion forms (a platform
  key signing the bytes; a FIDO2 credential through plain CTAP2 ``getAssertion`` with user verification);
- the enrollment MAC and the device fingerprint;
- the ``WSK``-signed device list;
- the host's ``stream_open`` gate (``remote.off`` / ``remote.device`` / ``remote.busy`` / ``remote.no_code``) and the
  version check of the open message (``remote.version``) as pure decision functions.

Refusals raise :class:`orch_hub.contract.hub.Refused` with the code of protocol.md §14.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from . import suite as S
from .canon import CanonError, cj, unb64u
from .canon import unhex as _canon_unhex
from .hub import LINK_WINDOW_MS, Refused, _envelope, _fields, _signature, link_label
from .phase4 import text_ok

# --- labels (protocol.md §3, "Remote access labels") ---------------------------------------------------------------

#: Every label is new, ends in ``|`` and keeps the union with every hub and v2 label prefix-free
#: (``tests/test_remote_vectors.py``; core: ``tests/host/test_remote_vectors.py`` against its live table).
REMOTE_LABELS: dict[str, str] = {
    # the presence key signs only these two (spec §5: the ``remote-presence`` backend kind, never a D64 person signer)
    "sig_remote_open": "orch/v2/remote-open|",
    "sig_remote_presence": "orch/v2/remote-presence|",
    # WSK signs the device list (host state, read by the click signer and the stream terminator)
    "sig_remote_devices": "orch/v2/sig/remote-devices|",
    # MAC and hashes (no signature)
    "mac_remote_enroll": "orch/v2/remote-enroll|",
    "h_remote_prologue": "orch/v2/remote-prologue|",
    "h_remote_wrk": "orch/v2/remote-wrk|",
    "h_remote_device_fp": "orch/v2/remote-device-fp|",
}
RL: dict[str, bytes] = {k: v.encode("ascii") for k, v in REMOTE_LABELS.items()}

# --- constants ------------------------------------------------------------------------------------------------------

NOISE_PROTOCOL = b"Noise_IK_25519_ChaChaPoly_SHA256"
REMOTE_VERSION = 1  # ``rv``: the remote contract version, bound in the prologue
KINDS = {"session": 1, "enroll": 2}
FRAME_MAX = 65535  # a Noise message, and so the decoded ``bytes`` of one ``stream_data``, is at most 65535 bytes
FRAME_MIN_COST = 1024  # every frame is charged at least this much against the byte budget (signature cost)
CHALLENGE_LEN = 32
CHALLENGE_TTL_MS = 60_000
NONCE_LEN = 32
BATCH_TTL_MS = 300_000
DIGESTS_MAX = 16
DEVICES_MAX = 64
DEVICE_LIFE_MAX_MS = 366 * 24 * 3600 * 1000
API_MAX = 2**31 - 1
#: §7.1 limits: per device, per workspace, handshakes per minute per device, enroll streams per minute per workspace
LIMITS = {"streams_per_device": 4, "streams_per_workspace": 16, "handshakes_per_min": 10, "enrolls_per_min": 3}
#: per stream and direction: charged bytes over the stream's life, and per rolling minute
BYTE_BUDGET = {"life": 1 << 30, "per_min": 64 << 20}
LEVELS = ("view", "work", "terminals")
PRESENCE_KINDS = {"platform": 1, "fido2": 2}
CTAP2_RP_ID = b"orch-remote"
CTAP2_UP, CTAP2_UV, CTAP2_AT, CTAP2_ED = 0x01, 0x04, 0x40, 0x80
CODES = ("remote.off", "remote.device", "remote.busy", "remote.no_code", "remote.version")
CLOSE_REASONS = (
    "closed",  # either side ended it normally (End, sign-out)
    "idle",  # 2 h idle (D91), 10 min for a terminal stream's last terminal
    "expired",  # 12 h session life, the remote policy's end date, or the device's expiry
    "revoked",  # turned off, level changed, device removed, WRK rotated (D91: same lock hold)
    "handshake",  # a handshake message did not open, or came out of order
    "presence",  # an open or presence signature did not verify
    "too_large",  # a frame over FRAME_MAX or the byte budget
    "malformed",  # a frame or message outside its exact shape
    *CODES,
)

DEVICE_ID = re.compile(r"rd_[0-9a-f]{32}")
CODE_ID = re.compile(r"rc_[0-9a-f]{32}")
PERSON = re.compile(r"p_[0-9a-f]{32}")


def _hex16(x: Any) -> bool:
    return type(x) is str and re.fullmatch(r"[0-9a-f]{32}", x) is not None


def _int_min(lo: int) -> Any:
    return lambda x: type(x) is int and lo <= x <= S_MAX_SAFE


S_MAX_SAFE = 2**53 - 1


def unhex(s: object, n: int) -> bytes:
    """Lower-case hex of exactly ``n`` bytes; anything else is ``malformed``."""
    try:
        return _canon_unhex(s, n)
    except CanonError:
        raise Refused("malformed") from None


def id_bytes(ident: str) -> bytes:
    """The 16 bytes behind ``rd_<32 hex>`` (a device) or ``rc_<32 hex>`` (an enrollment code)."""
    if type(ident) is not str or not (DEVICE_ID.fullmatch(ident) or CODE_ID.fullmatch(ident)):
        raise Refused("malformed")
    return bytes.fromhex(ident[3:])


# --- link stream ops (§10.2c) ---------------------------------------------------------------------------------------


def _frame_bytes(x: Any) -> bool:
    try:
        raw = unb64u(x)
    except CanonError:
        return False
    return 1 <= len(raw) <= FRAME_MAX


#: op -> (request direction: "h2w" or "both", exact body). ``both``: a one-way frame, ``re`` is always null, no reply.
STREAM_OPS: dict[str, tuple[str, dict[str, Any]]] = {
    "stream_open": (
        "h2w",
        {
            "stream_id": _hex16,
            "device_id": lambda x: type(x) is str and (DEVICE_ID.fullmatch(x) or CODE_ID.fullmatch(x)) is not None,
            "kind": lambda x: x in ("session", "enroll"),
        },
    ),
    "stream_data": ("both", {"stream_id": _hex16, "seq": _int_min(1), "bytes": _frame_bytes}),
    "stream_close": ("both", {"stream_id": _hex16, "reason": lambda x: type(x) is str and x in CLOSE_REASONS}),
}


def verify_stream_link(
    signed: Any, *, direction: str, hub_id_: str, workspace_id: str, pub: bytes, now_ms: int
) -> dict[str, Any]:
    """The §10.2 receiver steps for a stream op (both modes). ``pub``: WSK for w2h (the hub checks), the card's
    HK_op for h2w (the host checks). A non-stream op is ``unknown_op`` here (``hub.verify_link`` handles those)."""
    o = _envelope(signed, "hub.link")
    _signature(signed, link_label(direction), pub)
    _fields(o, "hub.link")
    if o["dir"] != direction:
        raise Refused("malformed")
    spec = STREAM_OPS.get(o["op"])
    if spec is None:
        raise Refused("unknown_op")
    req_dir, body = spec
    if o["re"] is not None or (req_dir != "both" and o["dir"] != req_dir):
        raise Refused("unknown_op")  # stream ops are requests only; a stream_open reply is an ordinary reply
    if o["hub_id"] != hub_id_:
        raise Refused("other_hub")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if abs(now_ms - o["ts_ms"]) > LINK_WINDOW_MS:
        raise Refused("stale_timestamp")
    b = o["body"]
    if set(b) != set(body) or not all(ok(b[k]) for k, ok in body.items()):
        raise Refused("malformed")
    if o["op"] == "stream_open":
        want = DEVICE_ID if b["kind"] == "session" else CODE_ID
        if want.fullmatch(b["device_id"]) is None:
            raise Refused("malformed")
    return o


def frame_cost(signed: Mapping[str, Any]) -> int:
    """What one stream frame costs against its stream's byte budget: its whole signed form, at least 1 KiB."""
    return max(FRAME_MIN_COST, len(cj(signed)))


# --- Noise_IK_25519_ChaChaPoly_SHA256 (Noise rev 34, §5 and §7.5) -------------------------------------------------


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _noise_hkdf(ck: bytes, ikm: bytes, n: int) -> list[bytes]:
    temp = _hmac(ck, ikm)
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = _hmac(temp, prev + bytes([i]))
        out.append(prev)
    return out


def _x25519_pub(priv: X25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def x25519_priv(raw: bytes) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(raw)


def x25519_pub(raw: bytes) -> bytes:
    return _x25519_pub(x25519_priv(raw))


def _dh(priv: X25519PrivateKey, pub: bytes) -> bytes:
    try:
        return priv.exchange(X25519PublicKey.from_public_bytes(pub))
    except ValueError:  # a low-order point gives the all-zero secret: refused, never used
        raise Refused("handshake") from None


@dataclass
class CipherState:
    k: bytes | None = None
    n: int = 0

    def _nonce(self) -> bytes:
        return b"\x00" * 4 + struct.pack("<Q", self.n)

    def encrypt(self, ad: bytes, pt: bytes) -> bytes:
        if self.k is None:
            return pt
        ct = ChaCha20Poly1305(self.k).encrypt(self._nonce(), pt, ad)
        self.n += 1
        return ct

    def decrypt(self, ad: bytes, ct: bytes) -> bytes:
        if self.k is None:
            return ct
        try:
            pt = ChaCha20Poly1305(self.k).decrypt(self._nonce(), ct, ad)
        except InvalidTag:
            raise Refused("handshake") from None
        self.n += 1
        return pt


@dataclass
class Symmetric:
    ck: bytes = b""
    h: bytes = b""
    cs: CipherState = field(default_factory=CipherState)

    @classmethod
    def start(cls, prologue: bytes, responder_static: bytes) -> Symmetric:
        h = NOISE_PROTOCOL if len(NOISE_PROTOCOL) == 32 else hashlib.sha256(NOISE_PROTOCOL).digest()
        st = cls(ck=h, h=h)
        st.mix_hash(prologue)
        st.mix_hash(responder_static)  # IK pre-message: <- s
        return st

    def mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def mix_key(self, ikm: bytes) -> None:
        self.ck, k = _noise_hkdf(self.ck, ikm, 2)
        self.cs = CipherState(k=k)

    def encrypt_and_hash(self, pt: bytes) -> bytes:
        ct = self.cs.encrypt(self.h, pt)
        self.mix_hash(ct)
        return ct

    def decrypt_and_hash(self, ct: bytes) -> bytes:
        pt = self.cs.decrypt(self.h, ct)
        self.mix_hash(ct)
        return pt

    def split(self) -> tuple[CipherState, CipherState]:
        k1, k2 = _noise_hkdf(self.ck, b"", 2)
        return CipherState(k=k1), CipherState(k=k2)


def prologue(kind: str, workspace_id: str, ident: str, stream_id: str, hub_id_: str) -> bytes:
    """``label || rv (1) || kind (1: session, 2: enroll) || workspace_id (16) || device or code id (16) ||
    stream_id (16) || hub_id (16)``: 83 bytes. A session needs an ``rd_`` id, an enrollment an ``rc_`` code id."""
    if kind not in KINDS or (DEVICE_ID if kind == "session" else CODE_ID).fullmatch(str(ident)) is None:
        raise Refused("malformed")
    return (
        RL["h_remote_prologue"]
        + bytes([REMOTE_VERSION, KINDS[kind]])
        + unhex(workspace_id, 16)
        + id_bytes(ident)
        + unhex(stream_id, 16)
        + unhex(hub_id_, 16)
    )


@dataclass
class Handshake:
    """One side of an IK handshake. ``hh`` (the handshake hash) and the transport ciphers exist after message 2."""

    sym: Symmetric
    s: X25519PrivateKey | None = None
    e: X25519PrivateKey | None = None
    rs: bytes = b""
    re_: bytes = b""
    hh: bytes = b""
    send: CipherState | None = None
    recv: CipherState | None = None


def initiator_msg1(prologue_: bytes, s: bytes, e: bytes, wrk_pub: bytes) -> tuple[Handshake, bytes]:
    """The device: ``-> e, es, s, ss`` with an EMPTY payload (no early data). 96 bytes."""
    hs = Handshake(sym=Symmetric.start(prologue_, wrk_pub), s=x25519_priv(s), e=x25519_priv(e), rs=wrk_pub)
    e_pub = _x25519_pub(hs.e)
    hs.sym.mix_hash(e_pub)
    hs.sym.mix_key(_dh(hs.e, wrk_pub))
    enc_s = hs.sym.encrypt_and_hash(_x25519_pub(hs.s))
    hs.sym.mix_key(_dh(hs.s, wrk_pub))
    return hs, e_pub + enc_s + hs.sym.encrypt_and_hash(b"")


def responder_read_msg1(prologue_: bytes, wrk: bytes, msg1: bytes) -> Handshake:
    """The host: read message 1 under its ``WRK``; the device's transport key is ``hs.rs``. Wrong size or a
    non-empty payload is ``malformed``; anything that does not open (another prologue, another WRK) ``handshake``."""
    if type(msg1) is not bytes or len(msg1) != 96:
        raise Refused("malformed")
    hs = Handshake(sym=Symmetric.start(prologue_, x25519_pub(wrk)), s=x25519_priv(wrk))
    hs.re_ = msg1[:32]
    hs.sym.mix_hash(hs.re_)
    hs.sym.mix_key(_dh(hs.s, hs.re_))
    hs.rs = hs.sym.decrypt_and_hash(msg1[32:80])
    hs.sym.mix_key(_dh(hs.s, hs.rs))
    if hs.sym.decrypt_and_hash(msg1[80:]) != b"":
        raise Refused("malformed")  # unreachable with 16 bytes left; kept so a later change cannot add early data
    return hs


def responder_msg2(hs: Handshake, e: bytes, challenge: bytes) -> bytes:
    """``<- e, ee, se`` with the 32-byte challenge as the payload. 80 bytes. Sets ``hh`` and the ciphers."""
    if len(challenge) != CHALLENGE_LEN:
        raise Refused("malformed")
    hs.e = x25519_priv(e)
    e_pub = _x25519_pub(hs.e)
    hs.sym.mix_hash(e_pub)
    hs.sym.mix_key(_dh(hs.e, hs.re_))
    hs.sym.mix_key(_dh(hs.e, hs.rs))
    out = e_pub + hs.sym.encrypt_and_hash(challenge)
    hs.hh = hs.sym.h
    c1, c2 = hs.sym.split()
    hs.recv, hs.send = c1, c2
    return out


def initiator_read_msg2(hs: Handshake, msg2: bytes) -> bytes:
    """The device reads message 2 and returns the challenge (exactly 32 bytes)."""
    if type(msg2) is not bytes or len(msg2) != 80:
        raise Refused("malformed")
    hs.re_ = msg2[:32]
    hs.sym.mix_hash(hs.re_)
    assert hs.e is not None and hs.s is not None
    hs.sym.mix_key(_dh(hs.e, hs.re_))
    hs.sym.mix_key(_dh(hs.s, hs.re_))
    challenge = hs.sym.decrypt_and_hash(msg2[32:])
    hs.hh = hs.sym.h
    c1, c2 = hs.sym.split()
    hs.send, hs.recv = c1, c2
    return challenge


# --- presence (§7.2, §7.3) ------------------------------------------------------------------------------------------


def open_bytes(workspace_id: str, ident: str, hh: bytes, challenge: bytes) -> bytes:
    """What the presence key signs to open a stream: ``remote-open| || workspace_id || device id (or code id) ||
    handshake_hash || challenge`` (16 + 16 + 32 + 32 bytes after the label)."""
    if len(hh) != 32 or len(challenge) != CHALLENGE_LEN:
        raise Refused("malformed")
    return RL["sig_remote_open"] + unhex(workspace_id, 16) + id_bytes(ident) + hh + challenge


def presence_bytes(workspace_id: str, device_id: str, hh: bytes, nonce: bytes, digests: list[bytes]) -> bytes:
    """``remote-presence| || workspace_id || device_id || handshake_hash || nonce (32) || n (1) || view_digest[n]``;
    1-16 digests of 32 bytes, sorted ascending and unique (a batch is a fixed list, D88)."""
    if (
        DEVICE_ID.fullmatch(str(device_id)) is None
        or len(hh) != 32
        or len(nonce) != NONCE_LEN
        or not 1 <= len(digests) <= DIGESTS_MAX
        or any(len(d) != 32 for d in digests)
        or list(digests) != sorted(set(digests))
    ):
        raise Refused("malformed")
    return (
        RL["sig_remote_presence"]
        + unhex(workspace_id, 16)
        + id_bytes(device_id)
        + hh
        + nonce
        + bytes([len(digests)])
        + b"".join(digests)
    )


def ctap2_rp_id_hash() -> bytes:
    return hashlib.sha256(CTAP2_RP_ID).digest()


def verify_assertion(presence_pub: bytes, presence_kind: str, msg: bytes, assertion: Any, held_count: int) -> int:
    """Check one presence assertion over ``msg``; return the sign count to record (``held_count`` for a platform key).

    - ``{"form": "p256", "sig"}``: a platform key (Secure Enclave with ``.userPresence``, TPM behind Windows Hello)
      signs ``msg`` itself (ECDSA P-256/SHA-256, raw ``r || s``).
    - ``{"form": "ctap2", "auth_data", "sig"}``: a FIDO2 credential through plain CTAP2 ``getAssertion`` with
      ``rpId = "orch-remote"`` and ``clientDataHash = SHA-256(msg)``; ``auth_data`` is exactly 37 bytes (rpIdHash,
      flags, counter, no extensions); the signature is over ``auth_data || SHA-256(msg)``; UP and UV must be set; a
      counter that is not above a held non-zero counter is a replay.

    The form must be the device's ``presence_kind`` (``factor_mismatch``): a FIDO2 device cannot downgrade to a bare
    signature. Order: shape, form, signature, then flags and counter (unauthenticated bytes never pick the code)."""
    if type(assertion) is not dict or type(assertion.get("form")) is not str:
        raise Refused("malformed")
    form = assertion["form"]
    keys = {"p256": {"form", "sig"}, "ctap2": {"form", "auth_data", "sig"}}.get(form)
    if keys is None or set(assertion) != keys:
        raise Refused("malformed")
    try:
        sig = unb64u(assertion["sig"], S.SIG_LEN)
        auth = unb64u(assertion["auth_data"], 37) if form == "ctap2" else b""
    except CanonError:
        raise Refused("malformed") from None
    if {"p256": "platform", "ctap2": "fido2"}[form] != presence_kind:
        raise Refused("factor_mismatch")
    if form == "p256":
        if not S.verify(presence_pub, sig, msg):
            raise Refused("bad_signature")
        return held_count
    if auth[:32] != ctap2_rp_id_hash() or not S.verify(presence_pub, sig, auth + hashlib.sha256(msg).digest()):
        raise Refused("bad_signature")
    flags, count = auth[32], int.from_bytes(auth[33:37], "big")
    if flags & (CTAP2_AT | CTAP2_ED):
        raise Refused("malformed")
    if flags & (CTAP2_UP | CTAP2_UV) != CTAP2_UP | CTAP2_UV:
        raise Refused("factor_too_weak")  # a touch without user verification is not enough (spec §5)
    if (held_count > 0 or count > 0) and count <= held_count:
        raise Refused("replay")
    return count


def verify_open(
    entry: Mapping[str, Any], workspace_id: str, ident: str, hh: bytes, challenge: bytes, assertion: Any, held: int
) -> int:
    """The open signature of a session (``ident`` = the device id) or an enrollment (``ident`` = the code id, with
    ``entry`` holding the keys the enroll message names)."""
    msg = open_bytes(workspace_id, ident, hh, challenge)
    return verify_assertion(_pub65(entry["presence_pub"]), entry["presence_kind"], msg, assertion, held)


def verify_presence(
    entry: Mapping[str, Any],
    workspace_id: str,
    hh: bytes,
    nonce: bytes,
    digests: list[bytes],
    assertion: Any,
    held: int,
    *,
    issued_nonces: Mapping[bytes, int],
    now_ms: int,
) -> int:
    """The click signer's check of an action presence (D88): the nonce is one the signer issued, unused and at most
    5 min old (``replay`` otherwise; the caller deletes it in the same step), then the assertion over the bytes."""
    issued = issued_nonces.get(nonce)
    if issued is None or not issued <= now_ms < issued + BATCH_TTL_MS:
        raise Refused("replay")
    msg = presence_bytes(workspace_id, entry["device_id"], hh, nonce, digests)
    return verify_assertion(_pub65(entry["presence_pub"]), entry["presence_kind"], msg, assertion, held)


def _pub65(v: Any) -> bytes:
    try:
        pub = unb64u(v, S.PUB_LEN)
    except CanonError:
        raise Refused("malformed") from None
    if not S.valid_public_key(pub):
        raise Refused("malformed")
    return pub


# --- enrollment (§5) ------------------------------------------------------------------------------------------------


def wrk_fp(wrk_pub: bytes) -> bytes:
    """The 16-byte fingerprint of the workspace's remote key that an enrollment code carries; the device accepts a
    ``WRK`` only when it hashes to it."""
    if len(wrk_pub) != 32:
        raise Refused("malformed")
    return hashlib.sha256(RL["h_remote_wrk"] + wrk_pub).digest()[:16]


def code_bytes(code_id: str, secret: bytes, wrk_pub: bytes) -> bytes:
    """The 48 bytes an enrollment code stands for: code id (16) || secret (16, the HMAC key) || WRK fingerprint (16).
    Its text form (words, checksum) is R2's and the remote client's, from these bytes."""
    if CODE_ID.fullmatch(str(code_id)) is None or len(secret) != 16:
        raise Refused("malformed")
    return id_bytes(code_id) + secret + wrk_fp(wrk_pub)


def enroll_mac(secret: bytes, hh: bytes, transport_pub: bytes, presence_pub: bytes, presence_kind: str) -> bytes:
    """``HMAC-SHA256(code secret, remote-enroll| || handshake_hash || transport_pub (32) || presence_pub (65) ||
    presence_kind (1))``: proves the code inside the enrollment handshake, bound to both device keys."""
    if len(secret) != 16 or len(hh) != 32 or len(transport_pub) != 32 or presence_kind not in PRESENCE_KINDS:
        raise Refused("malformed")
    if not S.valid_public_key(presence_pub):
        raise Refused("malformed")
    msg = RL["mac_remote_enroll"] + hh + transport_pub + presence_pub + bytes([PRESENCE_KINDS[presence_kind]])
    return _hmac(secret, msg)


def device_fp(transport_pub: bytes, presence_pub: bytes, presence_kind: str) -> bytes:
    """What the owner confirms at the workspace and the device shows: 32 bytes (both render the same words)."""
    if len(transport_pub) != 32 or not S.valid_public_key(presence_pub) or presence_kind not in PRESENCE_KINDS:
        raise Refused("malformed")
    return hashlib.sha256(
        RL["h_remote_device_fp"] + bytes([PRESENCE_KINDS[presence_kind]]) + transport_pub + presence_pub
    ).digest()


def check_enroll(
    msg: Any, *, secret: bytes, workspace_id: str, code_id: str, hh: bytes, challenge: bytes, transport_pub: bytes
) -> dict[str, Any]:
    """The host's check of the device's first transport message on an enroll stream. Order: shape (``malformed``),
    the MAC (``bad_signature``: a wrong try, 5 burn the code), then the presence signature over the open bytes with
    the code id (``bad_signature`` / ``factor_*``). Nothing is stored and nothing is shown before all of it passes.
    Returns the pending device (name, keys, kind, fingerprint) the owner is asked to confirm."""
    want = {"t", "name", "presence_kind", "presence_pub", "mac", "presence"}
    if type(msg) is not dict or set(msg) != want or msg["t"] != "enroll":
        raise Refused("malformed")
    if not text_ok(msg["name"], 64) or msg["presence_kind"] not in PRESENCE_KINDS:
        raise Refused("malformed")
    presence_pub = _pub65(msg["presence_pub"])
    try:
        mac = unb64u(msg["mac"], 32)
    except CanonError:
        raise Refused("malformed") from None
    good = enroll_mac(secret, hh, transport_pub, presence_pub, msg["presence_kind"])
    if not hmac.compare_digest(mac, good):
        raise Refused("bad_signature")
    entry = {"presence_pub": msg["presence_pub"], "presence_kind": msg["presence_kind"]}
    count = verify_open(entry, workspace_id, code_id, hh, challenge, msg["presence"], 0)
    return {
        "name": msg["name"],
        "presence_kind": msg["presence_kind"],
        "presence_pub": msg["presence_pub"],
        "transport_pub": transport_pub.hex(),
        "fingerprint": device_fp(transport_pub, presence_pub, msg["presence_kind"]).hex(),
        "sign_count": count,
    }


def check_session_static(entry: Mapping[str, Any], rs: bytes) -> None:
    """After message 1 of a session: the device's Noise static key must be the transport key its list entry names
    (``handshake`` otherwise, the stream closes before message 2). Not an authenticator (spec §5), a pin: a presence
    key never opens a session over another device's transport key."""
    try:
        want = unb64u(entry["transport_pub"], 32)
    except CanonError:
        raise Refused("malformed") from None
    if not hmac.compare_digest(want, rs):
        raise Refused("handshake")


def check_open_msg(msg: Any, host_api: tuple[int, int]) -> int:
    """The device's first transport message on a session stream: ``{"t": "open", "api": [min, max], "presence"}``.
    Returns the API version both support (the higher end of the overlap); ``remote.version`` if none. The presence
    assertion is checked by :func:`verify_open` afterwards."""
    if type(msg) is not dict or set(msg) != {"t", "api", "presence"} or msg["t"] != "open":
        raise Refused("malformed")
    api = msg["api"]
    if type(api) is not list or len(api) != 2 or not all(type(v) is int and 1 <= v <= API_MAX for v in api):
        raise Refused("malformed")
    if api[0] > api[1]:
        raise Refused("malformed")
    lo, hi = max(api[0], host_api[0]), min(api[1], host_api[1])
    if lo > hi:
        raise Refused("remote.version")
    return hi


# --- the device list (§5) -------------------------------------------------------------------------------------------

DEVICE_LIST_KIND = "remote.device_list"


def _b64u_n(n: int) -> Any:
    def ok(x: Any) -> bool:
        try:
            unb64u(x, n)
        except CanonError:
            return False
        return True

    return ok


ENTRY_FIELDS: dict[str, Any] = {
    "device_id": lambda x: type(x) is str and DEVICE_ID.fullmatch(x) is not None,
    "name": lambda x: text_ok(x, 64),
    "person": lambda x: type(x) is str and PERSON.fullmatch(x) is not None,
    "transport_pub": _b64u_n(32),
    "presence_pub": lambda x: _b64u_n(65)(x) and S.valid_public_key(unb64u(x, 65)),
    "presence_kind": lambda x: x in PRESENCE_KINDS,
    "level_cap": lambda x: x in LEVELS,
    "expires_ms": _int_min(0),
    "added_ms": _int_min(0),
    "confirmed_by": lambda x: type(x) is str and PERSON.fullmatch(x) is not None,
}
LIST_FIELDS: dict[str, Any] = {
    "workspace_id": _hex16,
    "version": _int_min(1),
    "issued_ms": _int_min(0),
    "devices": lambda x: type(x) is list and len(x) <= DEVICES_MAX,
}


def device_list_bytes(o: Mapping[str, Any]) -> bytes:
    return RL["sig_remote_devices"] + cj(o)


def verify_device_list(signed: Any, *, wsk_pub: bytes, workspace_id: str, held_version: int) -> dict[str, Any]:
    """§2.2 order: envelope; WSK signature under ``sig/remote-devices|``; exact fields; then: the list names this
    workspace (``other_workspace``); its version is not below the held one (``stale_version``; equal is the held
    list itself); entries sorted by ``device_id``, unique ids and keys, ``added_ms < expires_ms <= added_ms + 366 d``
    (``malformed``). Returns ``o``."""
    o = _envelope(signed, DEVICE_LIST_KIND)
    try:
        sig = unb64u(signed["sig"], S.SIG_LEN)
    except CanonError:
        raise Refused("malformed") from None
    if not S.verify(wsk_pub, sig, device_list_bytes(o)):
        raise Refused("bad_signature")
    if set(o) != {"v", "suite", "kind", "hv", *LIST_FIELDS} or not all(ok(o[k]) for k, ok in LIST_FIELDS.items()):
        raise Refused("malformed")
    for d in o["devices"]:
        if type(d) is not dict or set(d) != set(ENTRY_FIELDS) or not all(ok(d[k]) for k, ok in ENTRY_FIELDS.items()):
            raise Refused("malformed")
        if not d["added_ms"] < d["expires_ms"] <= d["added_ms"] + DEVICE_LIFE_MAX_MS:
            raise Refused("malformed")
    ids = [d["device_id"] for d in o["devices"]]
    keys = [d["transport_pub"] for d in o["devices"]] + [d["presence_pub"] for d in o["devices"]]
    if ids != sorted(set(ids)) or len(set(keys)) != len(keys):
        raise Refused("malformed")
    if o["workspace_id"] != workspace_id:
        raise Refused("other_workspace")
    if o["version"] < held_version:
        raise Refused("stale_version")
    return o


def device_entry(o: Mapping[str, Any], device_id: str, now_ms: int) -> dict[str, Any]:
    """The listed, unexpired entry for ``device_id`` (``remote.device`` otherwise)."""
    for d in o["devices"]:
        if d["device_id"] == device_id:
            if now_ms >= d["expires_ms"]:
                break
            return dict(d)
    raise Refused("remote.device")


# --- the host's stream_open gate (§7.1 step 1) ----------------------------------------------------------------------


def stream_open_gate(body: Mapping[str, Any], state: Mapping[str, Any], now_ms: int) -> None:
    """Pure form of the host's first answer to a verified ``stream_open``; nothing reaches the dashboard before it.

    ``state``: ``remote`` (``{"on": bool, "expires_ms": int | None}``), ``devices`` (the verified device list
    object), ``codes`` (outstanding code ids -> expiry ms), ``streams_device`` / ``streams_workspace`` (open
    streams), ``handshakes_device`` (handshakes of this device in the last minute), ``enrolls`` (enroll streams of
    the workspace in the last minute), ``seen_streams`` (stream ids used within the last 12 h).

    Order: a reused stream id (``replay``); enroll: outstanding code (``remote.no_code``), rate (``remote.busy``);
    session: on and unexpired (``remote.off``), device listed and unexpired (``remote.device``), limits
    (``remote.busy``)."""
    if body["stream_id"] in state["seen_streams"]:
        raise Refused("replay")
    if body["kind"] == "enroll":
        exp = state["codes"].get(body["device_id"])
        if exp is None or now_ms >= exp:
            raise Refused("remote.no_code")
        if state["enrolls"] >= LIMITS["enrolls_per_min"]:
            raise Refused("remote.busy")
        return
    remote = state["remote"]
    if not remote["on"] or (remote["expires_ms"] is not None and now_ms >= remote["expires_ms"]):
        raise Refused("remote.off")
    device_entry(state["devices"], body["device_id"], now_ms)
    if (
        state["streams_device"] >= LIMITS["streams_per_device"]
        or state["streams_workspace"] >= LIMITS["streams_per_workspace"]
        or state["handshakes_device"] >= LIMITS["handshakes_per_min"]
    ):
        raise Refused("remote.busy")
