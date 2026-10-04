"""Shared helpers of the chart types (bars, runs, spark, series, bullet, gantt, scores, ...): number formatting,
schema atoms and scale maths. A leading underscore keeps this module out of the core-type listing."""
from __future__ import annotations

import math

NUM = {"type": "number", "minimum": -1e12, "maximum": 1e12}
POS = {"type": "number", "minimum": 0, "maximum": 1e12}
UNIT = {"type": "string", "minLength": 1, "maxLength": 20}


def fmt(v) -> str:
    """A number without exponent or thousands separator: 286, 0.25, 1000000."""
    if float(v) == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.4f}".rstrip("0").rstrip(".")


def with_unit(v, unit: str | None) -> str:
    if not unit:
        return fmt(v)
    return f"{fmt(v)}{unit}" if unit in ("%", "°", "×") else f"{fmt(v)} {unit}"


def pct(v: float, lo: float, hi: float) -> float:
    """Where v sits between lo and hi, in percent (0 when the range is empty), clamped to 0..100."""
    return 0.0 if hi <= lo else max(0.0, min(100.0, (v - lo) / (hi - lo) * 100))


def p(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".") or "0"


def nice_ticks(lo: float, hi: float, want: int = 5) -> list[float]:
    """Round tick values covering lo..hi inside it (steps of 1, 2 or 5 times a power of ten)."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / want
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step) * step
    return [round(first + i * step, 10) for i in range(int((hi - first) / step + 1e-9) + 1)]


def duration(seconds: float) -> str:
    """94 -> 1m 34s; 3700 -> 1h 1m; 0.4 -> 0.4s."""
    if seconds < 10:
        return f"{fmt(round(seconds, 1))}s"
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s" if s % 60 else f"{s // 60}m"
    return f"{s // 3600}h {s % 3600 // 60}m" if s % 3600 // 60 else f"{s // 3600}h"
