"""Split the suite across CI jobs: ``ORCH_TEST_SHARD=i/n`` runs the i-th of n slices of the selected tests.

A slice holds whole modules, so a module-scoped fixture is built once per slice, never once per test. The split is a
function of the collected module names, their test counts and the checked-in ``_shard_weights.json`` alone, so every
job (and every xdist worker inside it) computes the same split and each selected test runs in exactly one slice.

The weights are seconds per module (``scripts/shard_weights.py`` rebuilds them from a ``--durations=0`` log). A module
the file does not know is weighted by its test count, so a stale file costs balance, never coverage."""

from __future__ import annotations

import json
from pathlib import Path

#: seconds per test for a module missing from the weights file (the suite averages about this)
FALLBACK_SECONDS_PER_TEST = 0.3
WEIGHTS_FILE = Path(__file__).with_name("_shard_weights.json")


def load_weights(path: Path = WEIGHTS_FILE) -> dict[str, float]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def module_weights(counts: dict[str, int], known: dict[str, float]) -> dict[str, float]:
    """Seconds per collected module: the recorded ones, else the test count times the fallback."""
    return {m: known.get(m, n * FALLBACK_SECONDS_PER_TEST) for m, n in counts.items()}


def parse(spec: str) -> tuple[int, int]:
    """``"2/3"`` -> ``(2, 3)``; anything else is a configuration error, never a silent full run."""
    try:
        index, count = (int(part) for part in spec.split("/"))
    except ValueError:
        raise ValueError(f"ORCH_TEST_SHARD must look like 2/3, not {spec!r}") from None
    if not 1 <= index <= count:
        raise ValueError(f"ORCH_TEST_SHARD index must be in 1..{count}, not {index}")
    return index, count


def assign(weights: dict[str, float], count: int) -> dict[str, int]:
    """Module -> slice (1..count): heaviest module first, each onto the lightest slice so far (ties: lowest)."""
    load = [0] * count
    out: dict[str, int] = {}
    for module, weight in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])):
        slot = min(range(count), key=lambda i: (load[i], i))
        load[slot] += weight
        out[module] = slot + 1
    return out
