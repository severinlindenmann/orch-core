"""The Noise responder of a remote access stream (R3, #1591; hub contract 10.2c "Handshake").

``Noise_IK_25519_ChaChaPoly_SHA256`` (Noise rev 34), responder side only: the workspace's remote key ``WRK`` answers a
device's transport key. Built from ``cryptography`` primitives; vectors ``msg1``, ``msg2_build``, ``transport`` and the
transcripts of ``vectors_remote.json`` are replayed against it (``tests/host/test_remote_session.py``).

- :func:`prologue` is the contract's prologue (kind, workspace, device or code id, stream, hub).
- :func:`read_msg1` opens message 1 (exactly 96 bytes, no early data) and returns the handshake and the device's static
  key; :func:`write_msg2` answers with a fresh ephemeral and the challenge and splits into a :class:`Transport`.
- :class:`Transport` seals and opens the transport messages (no associated data, nonce counting from 0 per direction)
  and can be :meth:`wiped <Transport.wipe>`: the keys are zeroed and every later call is refused.

Every refusal raises :class:`orch.hubclient.errors.HubRefused` (``malformed`` or ``handshake``). Nothing here logs."""

from __future__ import annotations

import hashlib
import hmac
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from orch.hubclient.errors import HubRefused

__all__ = ["MSG1_LEN", "MSG2_LEN", "PLAIN_MAX", "Handshake", "Transport", "prologue", "read_msg1", "write_msg2"]

PROTOCOL = b"Noise_IK_25519_ChaChaPoly_SHA256"  # exactly 32 bytes: the initial h and ck, used as is
LABEL = b"orch/v2/remote-prologue|"
RV = 1
KIND_BYTE = {"session": 1, "enroll": 2}
MSG1_LEN, MSG2_LEN = 96, 80
FRAME_MAX = 65535
PLAIN_MAX = FRAME_MAX - 16
_RD, _RC, _HEX32 = (re.compile(p) for p in (r"rd_[0-9a-f]{32}", r"rc_[0-9a-f]{32}", r"[0-9a-f]{32}"))


def _no(code: str = "malformed") -> HubRefused:
    return HubRefused(code)


def _hex16(s: object) -> bytes:
    if type(s) is not str or _HEX32.fullmatch(s) is None:
        raise _no()
    return bytes.fromhex(s)


def prologue(kind: str, workspace_id: str, ident: str, stream_id: str, hub_id: str) -> bytes:
    """``label || rv || kind || workspace (16) || device or code id (16) || stream (16) || hub (16)``."""
    if kind not in KIND_BYTE or (_RD if kind == "session" else _RC).fullmatch(str(ident)) is None:
        raise _no()
    return (
        LABEL + bytes([RV, KIND_BYTE[kind]]) + _hex16(workspace_id) + bytes.fromhex(ident[3:])
        + _hex16(stream_id) + _hex16(hub_id)
    )  # fmt: skip


def _mac(k: bytes, d: bytes) -> bytes:
    return hmac.new(k, d, hashlib.sha256).digest()


def _kdf(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    t = _mac(ck, ikm)
    a = _mac(t, b"\x01")
    return a, _mac(t, a + b"\x02")


def _pub(raw: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _dh(priv: bytes, pub: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(priv).exchange(X25519PublicKey.from_public_bytes(pub))
    except ValueError:  # a malformed or low-order public key (an all-zero shared secret)
        raise _no("handshake") from None


class _Cipher:
    """A ChaCha20-Poly1305 key with its message counter. ``k`` is a ``bytearray`` so :meth:`wipe` can zero it."""

    def __init__(self, k: bytes | None) -> None:
        self.k: bytearray | None = None if k is None else bytearray(k)
        self.n = 0

    def _go(self, ad: bytes, data: bytes, enc: bool) -> bytes:
        if self.k is None:
            return data
        nonce = bytes(4) + self.n.to_bytes(8, "little")
        aead = ChaCha20Poly1305(bytes(self.k))
        try:
            out = aead.encrypt(nonce, data, ad) if enc else aead.decrypt(nonce, data, ad)
        except InvalidTag:
            raise _no("handshake") from None
        self.n += 1
        return out

    def wipe(self) -> None:
        if self.k is not None:
            for i in range(len(self.k)):
                self.k[i] = 0
        self.k = None
        self.n = 1 << 64  # nothing opens or seals again


class Handshake:
    """The responder's state between message 1 and message 2."""

    def __init__(self, pro: bytes, wrk_pub: bytes) -> None:
        self.ck = self.h = PROTOCOL
        self.c = _Cipher(None)
        self.mix_h(pro)
        self.mix_h(wrk_pub)
        self.re = self.rs = b""

    def mix_h(self, d: bytes) -> None:
        self.h = hashlib.sha256(self.h + d).digest()

    def mix_k(self, ikm: bytes) -> None:
        self.ck, k = _kdf(self.ck, ikm)
        self.c = _Cipher(k)

    def enc(self, pt: bytes) -> bytes:
        ct = self.c._go(self.h, pt, True)  # noqa: SLF001
        self.mix_h(ct)
        return ct

    def dec(self, ct: bytes) -> bytes:
        pt = self.c._go(self.h, ct, False)  # noqa: SLF001
        self.mix_h(ct)
        return pt


class Transport:
    """The two directions after message 2. ``hh`` is the handshake hash the open and presence signatures bind."""

    def __init__(self, hh: bytes, c_i2r: _Cipher, c_r2i: _Cipher) -> None:
        self.hh, self._in, self._out = hh, c_i2r, c_r2i
        self.wiped = False

    def seal(self, pt: bytes) -> bytes:
        """Encrypt one host -> device message (<= 65519 bytes of plaintext)."""
        if self.wiped or len(pt) > PLAIN_MAX:
            raise _no("handshake" if self.wiped else "malformed")
        return self._out._go(b"", pt, True)  # noqa: SLF001

    def open(self, ct: bytes) -> bytes:
        """Decrypt one device -> host message; a changed, reflected, reordered or replayed one is ``handshake``."""
        if self.wiped or not 16 <= len(ct) <= FRAME_MAX:
            raise _no("handshake" if self.wiped else "malformed")
        return self._in._go(b"", ct, False)  # noqa: SLF001

    def wipe(self) -> None:
        self.wiped = True
        self._in.wipe()
        self._out.wipe()
        self.hh = b""


def read_msg1(pro: bytes, wrk: bytes, msg1: bytes) -> Handshake:
    """Open message 1 with the remote key's private scalar ``wrk``. Not 96 bytes: ``malformed`` (no early data).
    Another prologue, another ``WRK`` or a damaged message: ``handshake``. ``hs.rs`` is the device's static key."""
    if len(msg1) != MSG1_LEN:
        raise _no()
    hs = Handshake(pro, _pub(wrk))
    hs.re = msg1[:32]
    hs.mix_h(hs.re)
    hs.mix_k(_dh(wrk, hs.re))
    hs.rs = hs.dec(msg1[32:80])
    hs.mix_k(_dh(wrk, hs.rs))
    hs.dec(msg1[80:])
    return hs


def write_msg2(hs: Handshake, e: bytes, challenge: bytes) -> tuple[bytes, Transport]:
    """Message 2 (``e || enc(challenge)``, 80 bytes) from the host's fresh ephemeral scalar ``e`` and the 32-byte
    challenge, and the split transport."""
    if len(challenge) != 32:
        raise _no()
    ep = _pub(e)
    hs.mix_h(ep)
    hs.mix_k(_dh(e, hs.re))
    hs.mix_k(_dh(e, hs.rs))
    out = ep + hs.enc(challenge)
    k1, k2 = _kdf(hs.ck, b"")
    return out, Transport(hs.h, _Cipher(k1), _Cipher(k2))
