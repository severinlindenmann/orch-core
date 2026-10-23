"""D15b-1: the hub's own roster_v count (view/project.py) runs the shared `workspace_roster_v*` vectors that core runs
against its replay, so a full-mode build never meets a roster_v the host counts differently."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.view import project as PR

V = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json").read_text())
CASES = [c for c in V["cases"] if c["check"] == "roster_v"]


def test_there_are_roster_cases():
    assert {c["name"] for c in CASES} >= {"workspace_roster_v", "workspace_roster_v_genesis_only"}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_project_workspace_counts_like_core(case):
    assert PR.project_workspace(case["args"]["lines"]).roster_v == case["expect"]["ok"]["roster_v"]
