"""Hand-built packs (v2 with idx v2) and zlib bombs for the limit tests."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path


def zlib_bomb(prefix: bytes, zeros: int, unit: int = 1 << 20) -> bytes:
    """A valid zlib stream of ``prefix`` and ``zeros`` zero bytes (a multiple of ``unit``), built from identical
    full-flush blocks: about 1 MB for 1 GiB, made in milliseconds, nothing large ever in memory."""
    assert zeros % unit == 0
    c = zlib.compressobj(9)
    chunk = b"\0" * unit
    head = c.compress(prefix) + c.flush(zlib.Z_FULL_FLUSH)
    block = c.compress(chunk) + c.flush(zlib.Z_FULL_FLUSH)
    assert c.compress(chunk) + c.flush(zlib.Z_FULL_FLUSH) == block  # each block stands alone
    adler = zlib.adler32(prefix)
    for _ in range(zeros // unit):
        adler = zlib.adler32(chunk, adler)
    return head + block * (zeros // unit) + b"\x03\x00" + adler.to_bytes(4, "big")


def oid(kind: str, data: bytes) -> str:
    return hashlib.sha1(b"%s %d\0" % (kind.encode(), len(data)) + data).hexdigest()


def _hdr(kind: int, size: int) -> bytes:
    c = (kind << 4) | (size & 15)
    size >>= 4
    out = bytearray()
    while size:
        out.append(c | 0x80)
        c = size & 0x7F
        size >>= 7
    out.append(c)
    return bytes(out)


def _ofs(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        n -= 1
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def append_delta(src: bytes, extra: bytes) -> bytes:
    """A delta turning ``src`` into ``src + extra`` (one copy, one insert)."""
    n = len(src)
    copy = bytes([0x80 | 0x10 | 0x20 | 0x40]) + n.to_bytes(3, "little")
    return _varint(n) + _varint(n + len(extra)) + copy + bytes([len(extra)]) + extra


def write_pack(objects: Path, entries: list[tuple[str, bytes, bytes | None, int | None]]) -> list[int]:
    """Write ``pack-<sha>.pack``/``.idx`` into ``objects/pack``. ``entries``: ``(oid, payload, raw zlib stream or
    None, ofs base index or None)``; with a base the payload is a delta. Returns the offsets."""
    body = bytearray(b"PACK" + struct.pack(">II", 2, len(entries)))
    offsets: list[int] = []
    crcs: list[int] = []
    for _oid, payload, stream, base in entries:
        off = len(body)
        offsets.append(off)
        if base is None:
            head = _hdr(3, len(payload))
        else:
            head = _hdr(6, len(payload)) + _ofs(off - offsets[base])
        data = head + (stream if stream is not None else zlib.compress(payload))
        crcs.append(zlib.crc32(data))
        body += data
    pack_sha = hashlib.sha1(body).digest()
    body += pack_sha
    order = sorted(range(len(entries)), key=lambda i: entries[i][0])
    fan = [0] * 256
    for i in order:
        fan[bytes.fromhex(entries[i][0])[0]] += 1
    acc = 0
    for b in range(256):
        acc += fan[b]
        fan[b] = acc
    idx = bytearray(b"\377tOc" + struct.pack(">I", 2) + struct.pack(">256I", *fan))
    for i in order:
        idx += bytes.fromhex(entries[i][0])
    for i in order:
        idx += struct.pack(">I", crcs[i])
    for i in order:
        idx += struct.pack(">I", offsets[i])
    idx += pack_sha
    idx += hashlib.sha1(idx).digest()
    d = objects / "pack"
    d.mkdir(exist_ok=True)
    name = "pack-" + pack_sha.hex()
    (d / (name + ".pack")).write_bytes(bytes(body))
    (d / (name + ".idx")).write_bytes(bytes(idx))
    return offsets


def delta_chain(objects: Path, depth: int) -> list[str]:
    """A base blob and ``depth`` ofs deltas, each on the one before. Returns the blob ids, base first."""
    blobs = [b"base\n"]
    entries: list[tuple[str, bytes, bytes | None, int | None]] = [(oid("blob", blobs[0]), blobs[0], None, None)]
    for i in range(depth):
        nxt = blobs[-1] + b"x"
        entries.append((oid("blob", nxt), append_delta(blobs[-1], b"x"), None, i))
        blobs.append(nxt)
    write_pack(objects, entries)
    return [e[0] for e in entries]
