"""QR codes as a module matrix; the template draws the SVG, so no generated markup reaches the page."""
from __future__ import annotations

import functools


def _compute(text: str) -> tuple | None:
    import segno
    from segno import DataOverflowError
    try:
        code = segno.make(text, error="m", micro=False)
    except (DataOverflowError, ValueError):
        return None  # too long to encode: the template shows a Callout, never a 500
    return tuple(tuple(bool(v) for v in row) for row in code.matrix)


_matrix = functools.lru_cache(maxsize=64)(_compute)


def qr_matrix(text: str) -> list[list[bool]] | None:
    """The module matrix of `text`, or None when it does not fit in a QR code. Cached: fine for an
    addon's own, non-secret QR content (the same text is drawn on every page render)."""
    m = _matrix(str(text))
    return None if m is None else [list(r) for r in m]


def qr_matrix_uncached(text: str) -> list[list[bool]] | None:
    """Like `qr_matrix`, but never goes through the module-matrix cache: for core's own pairing-link
    QR, whose text carries a one-time secret that must not sit in process memory past this render."""
    m = _compute(str(text))
    return None if m is None else [list(r) for r in m]


def qr_runs(matrix) -> list[tuple[int, int, int]]:
    """(x, y, width) of every horizontal run of dark modules, so the template draws one <path>, not a <rect> each."""
    out = []
    for y, row in enumerate(matrix or ()):
        x, n = 0, len(row)
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                out.append((start, y, x - start))
            else:
                x += 1
    return out
