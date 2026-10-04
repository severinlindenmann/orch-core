from __future__ import annotations

import json
import re

from orch.core.constants import STATUSES
from orch.core.fsutil import atomic_write_text
from orch.core.locks import lock


def format_id(prefix: str, pad: int, n: int) -> str:
    return f"{prefix}-{n:0{pad}d}"


def id_pattern(prefix: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(prefix)}-(\d+)$", re.IGNORECASE)


def normalize_ref(ws, ref: str) -> str:
    ref = ref.strip()
    prefix, pad = ws.config["id"]["prefix"], ws.config["id"]["pad"]
    if ref.isdigit():
        return format_id(prefix, pad, int(ref))
    m = id_pattern(prefix).match(ref)
    if m:
        return format_id(prefix, pad, int(m.group(1)))
    return ref


def _max_existing(ws) -> int:
    pat = re.compile(rf"^{re.escape(ws.config['id']['prefix'])}-(\d+)", re.IGNORECASE)
    best = 0
    for status in STATUSES:
        d = ws.status_dir(status)
        if not d.is_dir():
            continue
        for p in d.iterdir():
            m = pat.match(p.name)
            if m:
                best = max(best, int(m.group(1)))
    return best


def next_id(ws) -> str:
    counter = ws.state_dir / "counter.json"
    with lock(ws, "counter"):
        try:
            n = int(json.loads(counter.read_text(encoding="utf-8"))["next"])
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
            n = 1
        n = max(n, _max_existing(ws) + 1)  # never reuse, even if the counter was reset
        atomic_write_text(counter, json.dumps({"next": n + 1}) + "\n")
    return format_id(ws.config["id"]["prefix"], ws.config["id"]["pad"], n)
