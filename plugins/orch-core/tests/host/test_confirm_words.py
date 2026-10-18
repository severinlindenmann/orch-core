"""The typed word is derived once (signer.confirm_word); the click view and the dashboard's hand-off page must show
exactly what the terminal accepts. The shared case list also runs in the dashboard's handoff.test.ts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.click import view as views
from orch.host import signer

SHARED = Path(__file__).resolve().parents[4] / "addons" / "dashboard" / "src" / "test" / "confirm-words.json"
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
def test_terminal_click_and_dashboard_words_are_one_word(case):
    p = _prepared(case)
    assert signer.confirm_word(p, _registry) == case["word"]  # what `orch sign` accepts
    assert views.word(p, _registry) == case["word"]  # what the one-click view shows
