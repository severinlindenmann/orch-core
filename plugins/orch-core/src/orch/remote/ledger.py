"""Which remote decisions were handled, how, and the events each applied one wrote (spec §6.4). One JSON line per
decision, under `state_dir/remote/`. It holds no key and no signature. `lock()` serialises verify-and-apply, so a
replay or a second decision on the same target can never slip in between the check and the write."""
from __future__ import annotations

import json
from pathlib import Path

from filelock import FileLock


def path(ws) -> Path:
    return ws.state_dir / "remote" / "ledger.jsonl"


def lock(ws) -> FileLock:
    path(ws).parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path(ws).with_suffix(".lock")), timeout=10)


def entries(ws) -> list[dict]:
    try:
        lines = path(ws).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def seen(ws, decision_id: str) -> bool:
    return any(e.get("decision_id") == decision_id for e in entries(ws))


def decided(ws, ticket: str, target_key: str) -> bool:
    return any(e.get("ticket") == ticket and e.get("target") == target_key and e.get("status") == "applied"
               for e in entries(ws))


def applied_seqs(ws) -> set[int]:
    """Every event seq an applied decision wrote (what `orch check` accepts as a verified phone event)."""
    out: set[int] = set()
    for e in entries(ws):
        if e.get("status") != "applied":
            continue
        for seq in [e.get("event_seq"), *(e.get("event_seqs") or [])]:
            if isinstance(seq, int):
                out.add(seq)
    return out


def append(ws, entry: dict) -> None:
    path(ws).parent.mkdir(parents=True, exist_ok=True)
    with path(ws).open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
