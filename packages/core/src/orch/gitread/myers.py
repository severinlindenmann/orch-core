"""A line diff and its unified hunks.

This module is an independent implementation, written from Eugene W. Myers, "An O(ND) Difference Algorithm and
Its Variations" (Algorithmica 1, 1986), including the linear-space "middle snake" refinement of the same paper, and
from the unified format as documented in the GNU diffutils manual. It is released under the project's licence
(see LICENSE). It is not derived from any other diff implementation.

* :func:`split_lines` cuts bytes into records (each with its ``\\n``; a last line without one is a record too).
* :func:`diff_flags` finds a shortest edit script (greedy forward/backward search, middle snake, divide and
  conquer) and returns which old lines are deleted and which new lines are inserted. A search budget bounds the
  work on huge, unrelated inputs: past it, the part of the text still undecided is replaced as a whole, which is a
  correct (if not minimal) script.
* :func:`hunks` and :func:`render` turn the flags into unified hunks with a configurable number of context lines.

Where several shortest scripts exist the choice may differ from other tools; every script is valid.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["Hunk", "diff_flags", "hunks", "render", "split_lines"]

_BUDGET = 4_000_000  # search steps one diff may spend before undecided parts are replaced as a whole


class _GiveUp(Exception):
    pass


def split_lines(data: bytes) -> list[bytes]:
    """Records: each line with its ``\\n``; a last line without one is a record too."""
    out = data.split(b"\n")
    last = out.pop()
    recs = [x + b"\n" for x in out]
    if last:
        recs.append(last)
    return recs


def _middle(a: Sequence[int], alo: int, ahi: int, b: Sequence[int], blo: int, bhi: int, budget: list[int]):
    """The middle snake of ``a[alo:ahi]`` against ``b[blo:bhi]``: ``(x0, y0, x1, y1)`` in absolute indexes, a run of
    matches from ``(x0, y0)`` to ``(x1, y1)`` lying on a shortest path."""
    n, m = ahi - alo, bhi - blo
    delta = n - m
    odd = delta & 1
    half = (n + m + 1) // 2
    off = half + 1
    vf = [0] * (2 * half + 3)
    vb = [0] * (2 * half + 3)
    for d in range(half + 1):
        budget[0] -= 2 * d + 2
        if budget[0] < 0:
            raise _GiveUp
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and vf[off + k - 1] < vf[off + k + 1]):
                x = vf[off + k + 1]
            else:
                x = vf[off + k - 1] + 1
            y = x - k
            x0, y0 = x, y
            while x < n and y < m and a[alo + x] == b[blo + y]:
                x += 1
                y += 1
            vf[off + k] = x
            if odd and delta - (d - 1) <= k <= delta + (d - 1) and x + vb[off + delta - k] >= n:
                return alo + x0, blo + y0, alo + x, blo + y
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and vb[off + k - 1] < vb[off + k + 1]):
                x = vb[off + k + 1]
            else:
                x = vb[off + k - 1] + 1
            y = x - k
            x0, y0 = x, y
            while x < n and y < m and a[ahi - 1 - x] == b[bhi - 1 - y]:
                x += 1
                y += 1
            vb[off + k] = x
            if not odd and -d <= delta - k <= d and x + vf[off + delta - k] >= n:
                return ahi - x, bhi - y, ahi - x0, bhi - y0
    raise AssertionError("no middle snake")  # pragma: no cover (the paper proves one exists)


def _solve(
    a: Sequence[int],
    alo: int,
    ahi: int,
    b: Sequence[int],
    blo: int,
    bhi: int,
    da: bytearray,
    ib: bytearray,
    budget: list[int],
) -> None:
    stack = [(alo, ahi, blo, bhi)]
    while stack:
        alo, ahi, blo, bhi = stack.pop()
        while alo < ahi and blo < bhi and a[alo] == b[blo]:
            alo += 1
            blo += 1
        while alo < ahi and blo < bhi and a[ahi - 1] == b[bhi - 1]:
            ahi -= 1
            bhi -= 1
        if alo == ahi:
            for j in range(blo, bhi):
                ib[j] = 1
            continue
        if blo == bhi:
            for i in range(alo, ahi):
                da[i] = 1
            continue
        try:
            x0, y0, x1, y1 = _middle(a, alo, ahi, b, blo, bhi, budget)
        except _GiveUp:
            for i in range(alo, ahi):
                da[i] = 1
            for j in range(blo, bhi):
                ib[j] = 1
            continue
        stack.append((alo, x0, blo, y0))
        stack.append((x1, ahi, y1, bhi))


def _slide(seq: Sequence[int], mark: bytearray) -> None:
    """Move each run of marked lines down while the line after it equals its first line and is not marked: the
    same script in another place (a block added among repeats is reported at its end, as readers expect)."""
    n = len(seq)
    i = 0
    while i < n:
        if not mark[i]:
            i += 1
            continue
        s = i
        while i < n and mark[i]:
            i += 1
        e = i
        while e < n and not mark[e] and seq[s] == seq[e]:
            mark[s] = 0
            mark[e] = 1
            s += 1
            e += 1
            if e < n and mark[e]:
                break  # touches the next run: leave it
        i = e


def diff_flags(a: Sequence[bytes], b: Sequence[bytes]) -> tuple[bytearray, bytearray]:
    """``(deleted, inserted)``: one flag per old line and per new line. The lines left unflagged pair up in order
    and are equal."""
    ids: dict[bytes, int] = {}
    ai = [ids.setdefault(x, len(ids)) for x in a]
    bi = [ids.setdefault(x, len(ids)) for x in b]
    da = bytearray(len(ai))
    ib = bytearray(len(bi))
    _solve(ai, 0, len(ai), bi, 0, len(bi), da, ib, [_BUDGET])
    _slide(ai, da)
    _slide(bi, ib)
    return da, ib


class Hunk:
    """One unified hunk: 0-based starts and lengths into old and new, and its lines as ``(op, record)``."""

    __slots__ = ("a_len", "a_start", "b_len", "b_start", "lines")

    def __init__(self, a_start: int, a_len: int, b_start: int, b_len: int, lines: list[tuple[str, bytes]]) -> None:
        self.a_start, self.a_len, self.b_start, self.b_len, self.lines = a_start, a_len, b_start, b_len, lines


def _blocks(da: bytearray, ib: bytearray) -> list[tuple[int, int, int, int]]:
    """Maximal changed runs as ``(a_start, a_end, b_start, b_end)``."""
    n, m = len(da), len(ib)
    out: list[tuple[int, int, int, int]] = []
    i = j = 0
    while i < n or j < m:
        if (i < n and da[i]) or (j < m and ib[j]):
            si, sj = i, j
            while i < n and da[i]:
                i += 1
            while j < m and ib[j]:
                j += 1
            out.append((si, i, sj, j))
        else:
            i += 1
            j += 1
    return out


def hunks(a: Sequence[bytes], b: Sequence[bytes], context: int = 3) -> list[Hunk]:
    da, ib = diff_flags(a, b)
    blocks = _blocks(da, ib)
    out: list[Hunk] = []
    k = 0
    while k < len(blocks):
        first = k
        while k + 1 < len(blocks) and blocks[k + 1][0] - blocks[k][1] <= 2 * context:
            k += 1
        last = k
        k += 1
        a_lo = max(blocks[first][0] - context, 0)
        a_hi = min(blocks[last][1] + context, len(a))
        b_lo = blocks[first][2] - (blocks[first][0] - a_lo)
        b_hi = blocks[last][3] + (a_hi - blocks[last][1])
        lines: list[tuple[str, bytes]] = []
        ai = a_lo
        for bi_ in range(first, last + 1):
            sa, ea, sb, eb = blocks[bi_]
            lines += [(" ", a[x]) for x in range(ai, sa)]
            lines += [("-", a[x]) for x in range(sa, ea)]
            lines += [("+", b[x]) for x in range(sb, eb)]
            ai = ea
        lines += [(" ", a[x]) for x in range(ai, a_hi)]
        out.append(Hunk(a_lo, a_hi - a_lo, b_lo, b_hi - b_lo, lines))
    return out


def _range(start: int, length: int) -> str:
    if length == 0:
        return f"{start},0"
    return f"{start + 1}" if length == 1 else f"{start + 1},{length}"


def _funcname(a: Sequence[bytes], before: int) -> bytes:
    """The nearest line above ``before`` that starts with a letter, ``_`` or ``$``, cut at 80 bytes."""
    for i in range(min(before, len(a)) - 1, -1, -1):
        line = a[i]
        c = line[:1]
        if c and (c.isalpha() and c.isascii() or c in (b"_", b"$")):
            return line.rstrip()[:80]
    return b""


def render(a: Sequence[bytes], b: Sequence[bytes], context: int = 3) -> tuple[bytes, int, int]:
    """The hunks as unified text with the number of added and deleted lines."""
    out: list[bytes] = []
    add = dele = 0
    for h in hunks(a, b, context):
        fn = _funcname(a, h.a_start)
        head = f"@@ -{_range(h.a_start, h.a_len)} +{_range(h.b_start, h.b_len)} @@".encode()
        out.append(head + (b" " + fn if fn else b"") + b"\n")
        for op, rec in h.lines:
            if op == "+":
                add += 1
            elif op == "-":
                dele += 1
            out.append(op.encode() + rec)
            if not rec.endswith(b"\n"):
                out.append(b"\n\\ No newline at end of file\n")
    return b"".join(out), add, dele
