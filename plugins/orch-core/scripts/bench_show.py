"""Manual benchmark (not part of CI): cold `orch show` with 1000 tickets.

Run: uv run python scripts/bench_show.py
Target from the spec: < 200 ms on a laptop.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from orch.core.events import Actor
from orch.core.ops import Ops
from orch.core.workspace import Workspace


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        home = Path(d) / "orchestrator"
        home.mkdir()
        (home / "config.json").write_text(json.dumps({"schema": 1, "customer": "bench", "id": {"prefix": "L", "pad": 4}}), encoding="utf-8")
        ops = Ops(Workspace.open(Path(d)), Actor("agent", "bench", "cli", "b"))
        for i in range(1000):
            ops.new(f"Ticket number {i}")
        env = {**os.environ, "ORCH_HOME": str(home)}
        cmd = ["orch", "show", "L-0500"]
        subprocess.run(cmd, env=env, capture_output=True, check=True)  # warm the index cache
        runs = []
        for _ in range(5):
            start = time.perf_counter()
            subprocess.run(cmd, env=env, capture_output=True, check=True)
            runs.append(time.perf_counter() - start)
        runs.sort()
        print(f"orch show: best {runs[0] * 1000:.0f} ms, median {runs[2] * 1000:.0f} ms (target < 200 ms)")


if __name__ == "__main__":
    main()
