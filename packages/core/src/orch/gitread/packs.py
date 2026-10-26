"""One pack (v2/v3) and its index (idx v2): the sorted name table, offsets (with the large-offset table) and the
pack file opened lazily. Sizes and bounds are checked as the index is read (``.idx`` at most 256 MiB, section 0.11).
Entry parsing, inflation and deltas live in :class:`orch.gitread.objects.ObjectStore`."""

from __future__ import annotations

import bisect
import os
import struct

from . import limits
from .errors import Unavailable
from .fs import Dir

__all__ = ["Pack"]

RAWLEN = {"sha1": 20, "sha256": 32}


class Pack:
    def __init__(self, packdir: Dir, stem: str, algo: str) -> None:
        self.packdir = packdir
        self.stem = stem
        self.raw = RAWLEN[algo]
        idx = packdir.read(stem + ".idx", limits.IDX_MAX)
        r = self.raw
        if len(idx) < 8 + 1024 + 2 * r or idx[:4] != b"\377tOc" or struct.unpack(">I", idx[4:8])[0] != 2:
            raise Unavailable(f"{stem}.idx is not a version 2 index")
        self.fanout = struct.unpack(">256I", idx[8 : 8 + 1024])
        if any(a > b for a, b in zip(self.fanout, self.fanout[1:], strict=False)):
            raise Unavailable(f"{stem}.idx has a broken fanout table")
        n = self.count = self.fanout[255]
        self.names_at = 8 + 1024
        self.crc_at = self.names_at + n * r
        self.off_at = self.crc_at + n * 4
        self.large_at = self.off_at + n * 4
        rest = len(idx) - self.large_at - 2 * r
        if rest < 0 or rest % 8 or rest // 8 > n:
            raise Unavailable(f"{stem}.idx has the wrong length")
        self.large_count = rest // 8
        self.idx = idx
        self.fd: int | None = None

    def name(self, i: int) -> bytes:
        a = self.names_at + i * self.raw
        return self.idx[a : a + self.raw]

    def position(self, raw: bytes) -> tuple[int, bool]:
        """Where ``raw`` is (or would be inserted) in the sorted name table, and whether it is there."""
        b = raw[0]
        lo = self.fanout[b - 1] if b else 0
        hi = self.fanout[b]

        class _Names:
            def __getitem__(s, i: int) -> bytes:  # noqa: N805
                return self.name(i)

            def __len__(s) -> int:  # noqa: N805
                return hi

        i = bisect.bisect_left(_Names(), raw, lo, hi)  # type: ignore[arg-type]
        return i, i < hi and self.name(i) == raw

    def offset(self, i: int) -> int:
        (v,) = struct.unpack(">I", self.idx[self.off_at + 4 * i : self.off_at + 4 * i + 4])
        if v & 0x80000000:
            j = v & 0x7FFFFFFF
            if j >= self.large_count:
                raise Unavailable(f"{self.stem}.idx has a bad large offset")
            (v,) = struct.unpack(">Q", self.idx[self.large_at + 8 * j : self.large_at + 8 * j + 8])
        return v

    def open(self) -> int:
        if self.fd is None:
            fd, st = self.packdir.open_file(self.stem + ".pack")
            head = os.pread(fd, 12, 0)
            if len(head) != 12 or head[:4] != b"PACK" or struct.unpack(">I", head[4:8])[0] not in (2, 3):
                os.close(fd)
                raise Unavailable(f"{self.stem}.pack is not a version 2 pack")
            if struct.unpack(">I", head[8:12])[0] != self.count:
                os.close(fd)
                raise Unavailable(f"{self.stem}.pack and its index disagree on the object count")
            self.size = st.st_size
            self.fd = fd
        return self.fd

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
