"""Rebuild tests/_shard_weights.json (seconds per test module) from a ``pytest --durations=0`` log.

    uv run pytest -q -m "not slow and not relay_t2" -n auto --durations=0 > durations.txt
    uv run python scripts/shard_weights.py durations.txt

Only the proportions matter, so a log from a busy machine is fine. A module missing from the file is weighted by its
test count (see tests/_shard.py), so a stale file only costs balance, never coverage."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(r"\s*([\d.]+)s (?:setup|call|teardown)\s+(\S+?)::")


def weights(log: str) -> dict[str, float]:
    seconds: dict[str, float] = defaultdict(float)
    for line in log.splitlines():
        match = LINE.match(line)
        if match:
            seconds[match[2]] += float(match[1])
    return {module: round(total, 1) for module, total in sorted(seconds.items())}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    out = Path(__file__).resolve().parent.parent / "tests" / "_shard_weights.json"
    table = weights(Path(argv[1]).read_text())
    out.write_text(json.dumps(table, indent=0, sort_keys=True) + "\n")
    print(f"{len(table)} modules, {sum(table.values()):.0f} s -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
