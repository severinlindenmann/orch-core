"""The output ring of one run (D-P2-30): the last ``size`` bytes of the PTY, addressed by stream offset.

Offsets count bytes since the run started. Writing never blocks and never fails: when the ring is full the oldest
bytes go. A reader asks for bytes ``from`` an offset and learns the oldest offset still kept (``start``), so a gap is
visible to it, never silent.
"""

from __future__ import annotations

import threading

__all__ = ["RING_BYTES", "Ring"]

RING_BYTES = 1024 * 1024


class Ring:
    def __init__(self, size: int = RING_BYTES) -> None:
        if size <= 0:
            raise ValueError("ring size must be positive")
        self.size = size
        self._buf = bytearray(size)
        self._end = 0  # stream offset after the last byte written
        self._lock = threading.Lock()

    @property
    def end(self) -> int:
        with self._lock:
            return self._end

    @property
    def start(self) -> int:
        with self._lock:
            return max(0, self._end - self.size)

    def write(self, data: bytes) -> None:
        """Append ``data``; every byte counts in the offsets, also those that fall out at once."""
        if not data:
            return
        with self._lock:
            if len(data) > self.size:
                self._end += len(data) - self.size
                data = data[-self.size :]
            n = len(data)
            pos = self._end % self.size
            first = min(n, self.size - pos)
            self._buf[pos : pos + first] = data[:first]
            if first < n:
                self._buf[0 : n - first] = data[first:]
            self._end += n

    def read(self, offset: int | None = None, limit: int | None = None) -> tuple[int, int, bytes]:
        """``(start, from, data)``: the oldest kept offset, the offset ``data`` begins at (``max(offset, start)``) and
        up to ``limit`` bytes from there. ``offset`` past the end returns no bytes."""
        with self._lock:
            start = max(0, self._end - self.size)
            begin = start if offset is None else min(max(offset, start), self._end)
            count = self._end - begin if limit is None else min(limit, self._end - begin)
            pos = begin % self.size
            first = min(count, self.size - pos)
            data = bytes(self._buf[pos : pos + first]) + bytes(self._buf[0 : count - first])
            return start, begin, data
