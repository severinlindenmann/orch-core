"""Split the suite across CI jobs: ``ORCH_TEST_SHARD=i/n`` runs the i-th of n slices of the selected tests.

A slice holds whole modules, so a module-scoped fixture is built once per slice, never once per test. The split is a
function of the collected module names and test counts alone, so every job (and every xdist worker inside it) computes
the same split and each selected test runs in exactly one slice."""

from __future__ import annotations


def parse(spec: str) -> tuple[int, int]:
    """``"2/3"`` -> ``(2, 3)``; anything else is a configuration error, never a silent full run."""
    try:
        index, count = (int(part) for part in spec.split("/"))
    except ValueError:
        raise ValueError(f"ORCH_TEST_SHARD must look like 2/3, not {spec!r}") from None
    if not 1 <= index <= count:
        raise ValueError(f"ORCH_TEST_SHARD index must be in 1..{count}, not {index}")
    return index, count


def assign(weights: dict[str, int], count: int) -> dict[str, int]:
    """Module -> slice (1..count): heaviest module first, each onto the lightest slice so far (ties: lowest)."""
    load = [0] * count
    out: dict[str, int] = {}
    for module, weight in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        slot = min(range(count), key=lambda i: (load[i], i))
        load[slot] += weight
        out[module] = slot + 1
    return out
