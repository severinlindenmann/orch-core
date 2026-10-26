"""orch.ops.actors against the tables of format doc 5.4 (only when the docs are in the checkout)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orch.ops.actors import EVENT_ACTORS

DOC = Path(__file__).resolve().parents[4] / "docs" / "architecture" / "orch-v2-ticket-format.md"
ROW = re.compile(r"^\| `([a-z_.]+)` \| ([A-Za-z0-9, ()]+?) \|")


def doc_rows() -> dict[str, str]:
    text = DOC.read_text(encoding="utf-8")
    start = text.index("### 5.4 Event types")
    end = text.index("### 5.5 ")
    rows = {}
    for line in text[start:end].splitlines():
        m = ROW.match(line)
        if m:
            rows.setdefault(m.group(1), m.group(2))
    return rows


@pytest.mark.skipif(not DOC.exists(), reason="docs are not part of this checkout")
def test_actor_table_matches_f1_5_4():
    rows = doc_rows()
    assert set(rows) == set(EVENT_ACTORS)
    for t, col in rows.items():
        letters = set(re.findall(r"\b[PAUH]\b", col))  # D (addon, from P2) is refused in P1
        assert letters == set(EVENT_ACTORS[t]), (t, col)
