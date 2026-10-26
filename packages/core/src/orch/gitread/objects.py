"""The object store: loose objects and packs (pack v2/v3 with idx v2), ofs and ref deltas, SHA-1 and SHA-256.

**Every object is checked against its id** before it is returned, so whatever is built from it (a tree walk, a diff)
is built from exactly the objects the ids name (D58). Sizes are bounded (section 0.11): an inflated object is at most
64 MiB and must equal its header (a stream that inflates further is refused after one byte too many: a zlib bomb
never costs more than the cap), a delta chain is at most 64 deep, an ``.idx`` at most 256 MiB.

Never read: ``objects/info/alternates``, ``objects/info/packs``, multi-pack indexes, commit graphs, bitmaps,
promisor and keep files. A missing object is :class:`~orch.gitread.errors.Unavailable`; nothing is fetched.
"""

from __future__ import annotations

import hashlib
import os
import re
import zlib
from collections import OrderedDict

from . import limits
from .errors import Unavailable
from .fs import Dir
from .packs import Pack as _Pack

__all__ = ["HEXLEN", "RAWLEN", "ObjectStore", "apply_delta", "object_id"]

HEXLEN = {"sha1": 40, "sha256": 64}
RAWLEN = {"sha1": 20, "sha256": 32}
TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_TYPE_NAMES = set(TYPES.values())
_OFS_DELTA, _REF_DELTA = 6, 7
_SIZE = re.compile(rb"0|[1-9][0-9]{0,19}")
_FALLBACK_ABBREV = 7


def object_id(algo: str, kind: str, data: bytes) -> str:
    h = hashlib.sha1() if algo == "sha1" else hashlib.sha256()
    h.update(b"%s %d\0" % (kind.encode(), len(data)))
    h.update(data)
    return h.hexdigest()


def _inflate(d: zlib._Decompress, data: bytes, want: int, out: list[bytes], got: int) -> int:
    """Feed ``data`` to ``d`` without ever producing more than ``want + 1`` bytes in total. Returns the new total."""
    buf = data
    while True:
        piece = d.decompress(buf, want + 1 - got)
        if piece:
            out.append(piece)
            got += len(piece)
        if got > want:
            raise Unavailable("an object inflates beyond its declared size")
        buf = d.unconsumed_tail
        if d.eof or (not buf and not piece):
            return got


def _varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if i >= len(data) or shift > 63:
            raise Unavailable("a malformed delta")
        c = data[i]
        i += 1
        value |= (c & 0x7F) << shift
        shift += 7
        if not c & 0x80:
            return value, i


def apply_delta(base: bytes, delta: bytes) -> bytes:
    src, i = _varint(delta, 0)
    dst, i = _varint(delta, i)
    if src != len(base):
        raise Unavailable("a delta for a base of another size")
    if dst > limits.OBJECT_MAX:
        raise Unavailable("a delta result over the object size limit")
    out = bytearray()
    n = len(delta)
    try:
        while i < n:
            c = delta[i]
            i += 1
            if c & 0x80:
                off = size = 0
                for k in range(4):
                    if c & (1 << k):
                        off |= delta[i] << (8 * k)
                        i += 1
                for k in range(3):
                    if c & (0x10 << k):
                        size |= delta[i] << (8 * k)
                        i += 1
                if size == 0:
                    size = 0x10000
                if off + size > len(base) or len(out) + size > dst:
                    raise Unavailable("a delta copies outside its base")
                out += base[off : off + size]
            elif c:
                if i + c > n or len(out) + c > dst:
                    raise Unavailable("a delta inserts past its end")
                out += delta[i : i + c]
                i += c
            else:
                raise Unavailable("a delta with a reserved opcode")
    except IndexError:
        raise Unavailable("a truncated delta") from None
    if len(out) != dst:
        raise Unavailable("a delta result of the wrong size")
    return bytes(out)


class ObjectStore:
    """``read(oid)`` → ``(type, data)``, hash-checked. ``close()`` releases the pack fds."""

    def __init__(self, objects: Dir, algo: str) -> None:
        self.objects = objects
        self.algo = algo
        self.hexlen = HEXLEN[algo]
        self.packs: list[_Pack] = []
        self._open_packs: OrderedDict[int, _Pack] = OrderedDict()
        self._cache: OrderedDict[tuple[int, int], tuple[int, bytes]] = OrderedDict()
        self._cache_bytes = 0
        self._packdir: Dir | None = None
        if objects.kind("pack") == "dir":
            self._packdir = objects.sub("pack")
            stems = sorted(
                n[:-4] for n in self._packdir.listdir() if n.startswith("pack-") and n.endswith(".idx") and "/" not in n
            )
            if len(stems) > limits.PACKS_MAX:
                raise Unavailable("too many packs")
            for stem in stems:
                if self._packdir.kind(stem + ".pack") is None:
                    continue  # an index whose pack is gone (git ignores it as well)
                self.packs.append(_Pack(self._packdir, stem, algo))

    def close(self) -> None:
        for p in self.packs:
            p.close()
        if self._packdir is not None:
            self._packdir.close()

    # -- lookups
    def _check_oid(self, oid: str) -> bytes:
        if len(oid) != self.hexlen or not all(c in "0123456789abcdef" for c in oid):
            raise Unavailable(f"not an object id: {oid!r}")
        return bytes.fromhex(oid)

    def _find_packed(self, raw: bytes) -> tuple[int, int] | None:
        for k, p in enumerate(self.packs):
            i, found = p.position(raw)
            if found:
                return k, p.offset(i)
        return None

    def read(self, oid: str, expect: str | None = None) -> tuple[str, bytes]:
        raw = self._check_oid(oid)
        hit = self._find_packed(raw)
        if hit is not None:
            kind, data = self._read_packed(*hit)
        else:
            got = self._read_loose(oid)
            if got is None:
                raise Unavailable(f"object {oid} is missing")
            kind, data = got
        if object_id(self.algo, kind, data) != oid:
            raise Unavailable(f"object {oid} does not match its id")
        if expect is not None and kind != expect:
            raise Unavailable(f"object {oid} is a {kind}, not a {expect}")
        return kind, data

    def exists(self, oid: str) -> bool:
        raw = self._check_oid(oid)
        if self._find_packed(raw) is not None:
            return True
        return self.objects.kind(f"{oid[:2]}/{oid[2:]}") == "file"

    # -- loose
    def _read_loose(self, oid: str) -> tuple[str, bytes] | None:
        rel = f"{oid[:2]}/{oid[2:]}"
        if self.objects.kind(oid[:2]) != "dir" or self.objects.kind(rel) is None:
            return None
        comp = self.objects.read(rel, limits.OBJECT_MAX + limits.MiB)
        d = zlib.decompressobj()
        try:
            head = d.decompress(comp, 64)
            nul = head.find(b"\0")
            if nul < 0:
                raise Unavailable(f"loose object {oid} has a malformed header")
            kind_b, sep, size_b = head[:nul].partition(b" ")
            kind = kind_b.decode("ascii", errors="replace")
            if not sep or kind not in _TYPE_NAMES or not _SIZE.fullmatch(size_b):
                raise Unavailable(f"loose object {oid} has a malformed header")
            size = int(size_b)
            if size > limits.OBJECT_MAX:
                raise Unavailable(f"loose object {oid} is larger than the object size limit")
            out = [head[nul + 1 :]]
            got = len(out[0])
            if got > size:
                raise Unavailable("an object inflates beyond its declared size")
            if not d.eof:
                got = _inflate(d, d.unconsumed_tail, size, out, got)
            if not d.eof or d.unused_data or got != size:
                raise Unavailable(f"loose object {oid} is truncated or has trailing data")
        except zlib.error:
            raise Unavailable(f"loose object {oid} is not valid zlib") from None
        return kind, b"".join(out)

    # -- packed
    def _pack_fd(self, k: int) -> int:
        p = self.packs[k]
        if k in self._open_packs:
            self._open_packs.move_to_end(k)
            return p.open()
        while len(self._open_packs) >= limits.OPEN_PACKS:
            _, old = self._open_packs.popitem(last=False)
            old.close()
        self._open_packs[k] = p
        return p.open()

    def _entry(self, k: int, off: int) -> tuple[int, int, int, int | bytes | None]:
        """``(type, size, data offset, base)``: base is an offset (ofs delta), a raw id (ref delta) or ``None``."""
        fd = self._pack_fd(k)
        p = self.packs[k]
        if off < 12 or off >= p.size:
            raise Unavailable("a pack offset outside the pack")
        head = os.pread(fd, 32 + p.raw, off)
        if not head:
            raise Unavailable("a truncated pack")
        try:
            i = 0
            c = head[i]
            i += 1
            kind = (c >> 4) & 7
            size = c & 15
            shift = 4
            while c & 0x80:
                c = head[i]
                i += 1
                size |= (c & 0x7F) << shift
                shift += 7
                if shift > 70:
                    raise Unavailable("a malformed pack entry")
            base: int | bytes | None = None
            if kind == _OFS_DELTA:
                c = head[i]
                i += 1
                rel = c & 0x7F
                while c & 0x80:
                    c = head[i]
                    i += 1
                    rel = ((rel + 1) << 7) | (c & 0x7F)
                    if rel > off:
                        break
                if rel <= 0 or rel > off - 12:
                    raise Unavailable("an ofs delta whose base lies outside the pack")
                base = off - rel
            elif kind == _REF_DELTA:
                base = bytes(head[i : i + p.raw])
                if len(base) != p.raw:
                    raise Unavailable("a truncated pack")
                i += p.raw
        except IndexError:
            raise Unavailable("a truncated pack entry") from None
        return kind, size, off + i, base

    def _inflate_at(self, k: int, start: int, size: int) -> bytes:
        if size > limits.OBJECT_MAX:
            raise Unavailable("a pack entry larger than the object size limit")
        fd = self._pack_fd(k)
        d = zlib.decompressobj()
        out: list[bytes] = []
        got = 0
        pos = start
        try:
            while not d.eof:
                chunk = os.pread(fd, 65536, pos)
                if not chunk:
                    raise Unavailable("a truncated pack entry")
                pos += len(chunk)
                got = _inflate(d, chunk, size, out, got)
        except zlib.error:
            raise Unavailable("a pack entry is not valid zlib") from None
        if got != size:
            raise Unavailable("a pack entry of the wrong size")
        return b"".join(out)

    def _cache_put(self, key: tuple[int, int], value: tuple[int, bytes]) -> None:
        if key in self._cache or len(value[1]) > limits.CACHE_BYTES // 4:
            return
        self._cache[key] = value
        self._cache_bytes += len(value[1])
        while self._cache_bytes > limits.CACHE_BYTES:
            _, (_, old) = self._cache.popitem(last=False)
            self._cache_bytes -= len(old)

    def _read_packed(self, k: int, off: int) -> tuple[str, bytes]:
        chain: list[tuple[int, bytes]] = []
        held = 0
        cur = off
        while True:
            cached = self._cache.get((k, cur))
            if cached is not None:
                self._cache.move_to_end((k, cur))
                kind, data = cached
                break
            kind, size, start, base = self._entry(k, cur)
            if kind in TYPES:
                data = self._inflate_at(k, start, size)
                self._cache_put((k, cur), (kind, data))
                break
            if kind not in (_OFS_DELTA, _REF_DELTA):
                raise Unavailable("a pack entry of an unknown type")
            if len(chain) >= limits.DELTA_DEPTH:
                raise Unavailable(f"a delta chain deeper than {limits.DELTA_DEPTH}")
            delta = self._inflate_at(k, start, size)
            held += len(delta)
            if held > limits.DELTA_BYTES_MAX:
                raise Unavailable("a delta chain over the memory limit")
            chain.append((cur, delta))
            if isinstance(base, int):
                cur = base
            else:
                assert isinstance(base, bytes)
                i, found = self.packs[k].position(base)
                if not found:
                    raise Unavailable("a ref delta whose base is not in its pack")
                cur = self.packs[k].offset(i)
        for at, delta in reversed(chain):
            data = apply_delta(data, delta)
            self._cache_put((k, at), (kind, data))
        return TYPES[kind], data

    # -- abbreviations, as git abbreviates in a diff's index line
    def abbrev(self, oid: str) -> str:
        count = sum(p.count for p in self.packs)
        length = max(_FALLBACK_ABBREV, ((count.bit_length() - 1 if count else 0) + 1 + 1) // 2)
        if length >= self.hexlen:
            return oid
        raw = bytes.fromhex(oid)
        cur = length

        def extend(other: str, init: int) -> None:
            nonlocal cur
            i = init
            while i < self.hexlen and oid[i] == other[i]:
                i += 1
            if i < 32 and i >= cur:  # git compares with GIT_MAX_RAWSZ (a quirk kept as is)
                cur = i + 1

        for p in self.packs:
            i, found = p.position(raw)
            if not found:
                if i < p.count:
                    extend(p.name(i).hex(), 0)
            elif i < p.count - 1:
                extend(p.name(i + 1).hex(), 0)
            if i > 0:
                extend(p.name(i - 1).hex(), 0)
        if self.objects.kind(oid[:2]) == "dir":
            with self.objects.sub(oid[:2]) as sub:
                names = sub.listdir()
            init = cur
            prefix = oid[2:cur]
            for n in names:
                if len(n) == self.hexlen - 2 and n.startswith(prefix):
                    extend(oid[:2] + n, init)
        return oid[:cur]
