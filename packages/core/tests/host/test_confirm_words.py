"""The terminal's typed word (``orch sign``) is derived once (signer.confirm_word). #1198 C1: the one-click view has no
word any more; the shared case list still runs in the dashboard's hand-off page test (handoff.test.ts)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.host import signer

SHARED = (
    Path(__file__).resolve().parents[4] / "packages" / "addons" / "dashboard" / "src" / "test" / "confirm-words.json"
)
DATA = json.loads(SHARED.read_text())
WS = DATA["workspace"]


def _registry(wid: str):
    return ("acme", WS["prefix"]) if wid == WS["id"] else None


def _prepared(case: dict) -> dict:
    return {
        "op": case["op"],
        "workspace_id": WS["id"],
        "log": "workspace",
        "material": {"ticket": None},
        "events": [case["event"]],
    }


@pytest.mark.parametrize("case", DATA["cases"], ids=lambda c: c["op"])
def test_the_terminal_word(case):
    p = _prepared(case)
    assert signer.confirm_word(p, _registry) == case["word"]  # what `orch sign` accepts
