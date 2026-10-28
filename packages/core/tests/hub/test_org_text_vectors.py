"""M3 (#1430): the host refuses company skill text exactly as the hub does: the ``text_cases`` of the shared
``vectors_org_skills.json`` (hub and core run the same file), one case per class of hidden character."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch.hubclient.orgtext import skill_problem

VEC = json.loads((Path(__file__).resolve().parents[4] / "contracts/hub/vectors_org_skills.json").read_text())


@pytest.mark.parametrize("case", VEC["text_cases"], ids=[c["name"] for c in VEC["text_cases"]])
def test_text_case(case: dict) -> None:
    assert skill_problem(case["skill_name"], case["text"]) == case["expect"]


def test_every_hidden_class_is_in_the_vectors() -> None:
    names = {c["name"] for c in VEC["text_cases"]}
    assert {"variation selector", "tag character", "hangul filler", "line separator"} <= names


@pytest.mark.parametrize("case", VEC["mcp_cases"], ids=[c["name"] for c in VEC["mcp_cases"]])
def test_mcp_case(case: dict) -> None:
    from orch.hubclient.orgtext import mcp_problem

    assert mcp_problem(case["entry"]) == case["expect"]
