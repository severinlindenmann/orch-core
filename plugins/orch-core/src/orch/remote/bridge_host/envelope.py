"""The envelope's bytes (spec §3): header layout, plaintext framing, canonical JSON and the idempotency digest.
Nothing here needs a key."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from dataclasses import dataclass

MAGIC = b"SHRB"
VERSION = 1
TO_HOST, TO_DEVICE = 1, 2
F_LAST, F_STREAM, F_REFUSAL = 0x01, 0x02, 0x04
HEADER_LEN, TAG_LEN, SIG_LEN = 104, 16, 64
OVERHEAD = HEADER_LEN + TAG_LEN + SIG_LEN  # 184
MAX_REQUEST = 1 << 20  # a whole request envelope, decoded (the mailbox's limit)
MAX_CHUNK = 256 * 1024  # a whole response chunk envelope
MAX_META = 64 * 1024
ZERO_ID = bytes(16)
KEY_VERSION = 1  # the relay account's master-key version this host speaks; any other is dropped

_HDR = struct.Struct(">4sBBBB16s16s16s16sQQ16s")
assert _HDR.size == HEADER_LEN


class Malformed(ValueError):
    """The plaintext framing or the meta JSON is not valid."""


@dataclass(frozen=True)
class Header:
    direction: int
    flags: int
    workspace: bytes
    device: bytes
    rid: bytes
    stream: bytes
    seq: int
    ts_ms: int
    salt: bytes
    key_version: int = KEY_VERSION
    version: int = VERSION
    magic: bytes = MAGIC

    def encode(self) -> bytes:
        for name in ("workspace", "device", "rid", "stream", "salt"):
            if len(getattr(self, name)) != 16:
                raise ValueError(f"header field {name} is 16 bytes")
        return _HDR.pack(self.magic, self.version, self.direction, self.flags, self.key_version, self.workspace,
                         self.device, self.rid, self.stream, self.seq, self.ts_ms, self.salt)

    @classmethod
    def decode(cls, raw: bytes) -> "Header":
        if len(raw) < HEADER_LEN:
            raise ValueError("short header")
        m, v, d, f, kv, ws, dev, rid, st, seq, ts, salt = _HDR.unpack(raw[:HEADER_LEN])
        return cls(d, f, ws, dev, rid, st, seq, ts, salt, kv, v, m)


def split(env: bytes) -> tuple[bytes, bytes, bytes]:
    """header, ciphertext || tag, signature."""
    return env[:HEADER_LEN], env[HEADER_LEN:-SIG_LEN], env[-SIG_LEN:]


def digest(env: bytes) -> bytes:
    """The idempotency digest H(header || ciphertext || tag): the signature is left out, it is malleable (§3.4)."""
    hb, body, _ = split(env)
    return hashlib.sha256(hb + body).digest()


def canonical_json(obj) -> bytes:
    """The protocol's canonical JSON: sorted keys, no spaces, UTF-8 (not ASCII-escaped)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def frame(meta: dict, data: bytes = b"") -> bytes:
    """plaintext = meta_len (4) || canonical meta || data."""
    m = canonical_json(meta)
    if len(m) > MAX_META:
        raise ValueError("meta over 64 KiB")
    return struct.pack(">I", len(m)) + m + data


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise Malformed("duplicate key in meta")
        out[k] = v
    return out


def _no_constant(name):
    raise Malformed(f"{name} is not JSON")


def unframe(pt: bytes) -> tuple[dict, bytes]:
    """(meta, data), or Malformed: a meta_len above 64 KiB or beyond the plaintext, meta that is not strict UTF-8
    JSON, not an object, or has a duplicate key or a non-finite number."""
    if len(pt) < 4:
        raise Malformed("short plaintext")
    n = struct.unpack(">I", pt[:4])[0]
    if n > MAX_META or 4 + n > len(pt):
        raise Malformed("bad meta length")
    try:
        meta = json.loads(pt[4:4 + n].decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise Malformed("meta is not JSON") from e
    if not isinstance(meta, dict):
        raise Malformed("meta is not an object")
    return meta, pt[4 + n:]


_B64U = re.compile(r"[A-Za-z0-9_-]*")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def unb64u(s) -> bytes:
    """Strict base64url without padding: only the canonical spelling of some bytes is accepted."""
    if not isinstance(s, str) or not _B64U.fullmatch(s) or len(s) % 4 == 1:
        raise ValueError("not base64url")
    raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    if b64u(raw) != s:
        raise ValueError("not canonical base64url")
    return raw


_HEX = re.compile(r"[0-9a-f]*")


def unhex(s, n: int | None = None) -> bytes:
    """Lower-case hex of exactly `n` bytes (any length when None), or ValueError."""
    if not isinstance(s, str) or not _HEX.fullmatch(s) or len(s) % 2 or (n is not None and len(s) != 2 * n):
        raise ValueError("not lower-case hex of the right length")
    return bytes.fromhex(s)
