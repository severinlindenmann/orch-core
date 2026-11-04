"""The Noise initiator of a remote access stream (R5, #1593; hub contract 10.2c "Handshake").

``Noise_IK_25519_ChaChaPoly_SHA256`` (Noise rev 34), the device's side: the transport key ``s`` knows the workspace's
remote key ``WRK`` (``rs``, learnt from the enrollment code) and opens with message 1 (``e, es, s, ss``, an empty
payload, exactly 96 bytes); message 2 (``e, ee, se``, 80 bytes) carries the host's 32-byte challenge. After it the
handshake hash ``hh`` is what the open and presence signatures bind, and the transport ciphers are split.

Written from the contract and the shared vectors (``vectors_remote.json``), apart from the host's responder
(``orch.host.remote.noise``): the two sides do not share code. Every failure is :class:`ClientError` ``handshake``
(a message that does not open) or ``malformed`` (a wrong size). Nothing here logs."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from .codes import ClientError

__all__ = ["MSG1_LEN", "MSG2_LEN", "PLAIN_MAX", "Initiator", "Transport", "prologue", "x25519_public"]

PROTOCOL = b"Noise_IK_25519_ChaChaPoly_SHA256"  # exactly 32 bytes: the initial h and ck, used as is
LABEL = b"orch/v2/remote-prologue|"
RV = 1
KIND_BYTE = {"session": 1, "enroll": 2}
MSG1_LEN, MSG2_LEN = 96, 80
FRAME_MAX = 65535
PLAIN_MAX = FRAME_MAX - 16
_RD, _RC, _HEX32 = (re.compile(p) for p in (r"rd_[0-9a-f]{32}", r"rc_[0-9a-f]{32}", r"[0-9a-f]{32}"))


def _hex16(s: object) -> bytes:
    if type(s) is not str or _HEX32.fullmatch(s) is None:
        raise ClientError("malformed")
    return bytes.fromhex(s)


def prologue(kind: str, workspace_id: str, ident: str, stream_id: str, hub_id: str) -> bytes:
    """``label || rv || kind || workspace (16) || device or code id (16) || stream (16) || hub (16)``: 83 bytes."""
    if kind not in KIND_BYTE or (_RD if kind == "session" else _RC).fullmatch(str(ident)) is None:
        raise ClientError("malformed")
    return (
        LABEL + bytes([RV, KIND_BYTE[kind]]) + _hex16(workspace_id) + bytes.fromhex(ident[3:])
        + _hex16(stream_id) + _hex16(hub_id)
    )  # fmt: skip


def _mac(k: bytes, d: bytes) -> bytes:
    return hmac.new(k, d, hashlib.sha256).digest()


def _hkdf2(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    t = _mac(ck, ikm)
    a = _mac(t, b"\x01")
    return a, _mac(t, a + b"\x02")


def x25519_public(raw: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _dh(priv: bytes, pub: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(priv).exchange(X25519PublicKey.from_public_bytes(pub))
    except ValueError:  # a malformed or low-order public key (an all-zero shared secret)
        raise ClientError("handshake") from None


class _Cipher:
    def __init__(self, k: bytes | None) -> None:
        self.k: bytearray | None = None if k is None else bytearray(k)
        self.n = 0
        self.wiped = False

    def go(self, ad: bytes, data: bytes, enc: bool) -> bytes:
        if self.wiped:  # a wiped cipher must never fall back to "no key yet" (plaintext)
            raise ClientError("handshake")
        if self.k is None:
            return data
        nonce = bytes(4) + self.n.to_bytes(8, "little")
        aead = ChaCha20Poly1305(bytes(self.k))
        try:
            out = aead.encrypt(nonce, data, ad) if enc else aead.decrypt(nonce, data, ad)
        except InvalidTag:
            raise ClientError("handshake") from None
        self.n += 1
        return out

    def wipe(self) -> None:
        if self.k is not None:
            for i in range(len(self.k)):
                self.k[i] = 0
        self.k = None
        self.wiped = True
        self.n = 1 << 64


class Transport:
    """The two directions after message 2. ``hh`` is the handshake hash the open and presence signatures bind.
    ``seal`` is device -> host, ``open`` is host -> device; no associated data, nonces count from 0 per direction."""

    def __init__(self, hh: bytes, send: _Cipher, recv: _Cipher) -> None:
        self.hh, self._send, self._recv = hh, send, recv
        self.wiped = False

    def seal(self, pt: bytes) -> bytes:
        if self.wiped or len(pt) > PLAIN_MAX:
            raise ClientError("handshake" if self.wiped else "malformed")
        return self._send.go(b"", pt, True)

    def open(self, ct: bytes) -> bytes:
        if self.wiped or not 16 <= len(ct) <= FRAME_MAX:
            raise ClientError("handshake" if self.wiped else "malformed")
        return self._recv.go(b"", ct, False)

    def wipe(self) -> None:
        self.wiped = True
        self._send.wipe()
        self._recv.wipe()
        self.hh = b""


class Initiator:
    """One handshake from the device's side. ``msg1()`` first, then ``read_msg2(bytes)`` -> ``(challenge,
    transport)``."""

    def __init__(self, pro: bytes, s: bytes, wrk_pub: bytes, rand: Callable[[int], bytes] | None = None) -> None:
        import secrets

        if len(s) != 32 or len(wrk_pub) != 32:
            raise ClientError("malformed")
        self._s = s
        self._e = (rand or secrets.token_bytes)(32)
        self._rs = wrk_pub
        self.ck = self.h = PROTOCOL
        self._c = _Cipher(None)
        self._mix_h(pro)
        self._mix_h(wrk_pub)  # IK pre-message: <- s
        self._sent = False

    def _mix_h(self, d: bytes) -> None:
        self.h = hashlib.sha256(self.h + d).digest()

    def _mix_k(self, ikm: bytes) -> None:
        self.ck, k = _hkdf2(self.ck, ikm)
        self._c = _Cipher(k)

    def _enc(self, pt: bytes) -> bytes:
        ct = self._c.go(self.h, pt, True)
        self._mix_h(ct)
        return ct

    def _dec(self, ct: bytes) -> bytes:
        pt = self._c.go(self.h, ct, False)
        self._mix_h(ct)
        return pt

    def msg1(self) -> bytes:
        """``-> e, es, s, ss`` with an EMPTY payload (no early data). 96 bytes."""
        if self._sent:
            raise ClientError("handshake")
        self._sent = True
        ep = x25519_public(self._e)
        self._mix_h(ep)
        self._mix_k(_dh(self._e, self._rs))
        es = self._enc(x25519_public(self._s))
        self._mix_k(_dh(self._s, self._rs))
        return ep + es + self._enc(b"")

    def read_msg2(self, msg2: bytes) -> tuple[bytes, Transport]:
        """``<- e, ee, se``; returns the 32-byte challenge and the split transport (``transport.hh`` is set)."""
        if not self._sent or type(msg2) is not bytes or len(msg2) != MSG2_LEN:
            raise ClientError("malformed")
        re_ = msg2[:32]
        self._mix_h(re_)
        self._mix_k(_dh(self._e, re_))
        self._mix_k(_dh(self._s, re_))
        challenge = self._dec(msg2[32:])
        if len(challenge) != 32:
            raise ClientError("handshake")
        k1, k2 = _hkdf2(self.ck, b"")
        return challenge, Transport(self.h, _Cipher(k1), _Cipher(k2))
