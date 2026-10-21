"""Both runtimes consume the dashboard-authored results in the same fixture file."""

import json
import os
from pathlib import Path

import pytest

from orch.widgets import parse_block, parse_section, resolve_ticket_widgets, segments_to_json, spec_to_json

PATH = Path(__file__).resolve().parents[4] / "addons/dashboard/src/app/pages/ticket/widgets/parity-fixtures.json"
if not PATH.exists():
    if os.environ.get("CI"):
        raise RuntimeError(f"Shared widget parity fixtures are required in CI: {PATH}")
    pytest.skip(f"Shared dashboard widget parity fixtures are absent: {PATH}", allow_module_level=True)
FIXTURES = json.loads(PATH.read_text())


def expand(case):
    if "gen" in case:
        g = case["gen"]
        return g["prefix"] + g["unit"] * g["count"] + g["suffix"]
    return case.get("raw", case.get("text", ""))


@pytest.mark.parametrize("case", FIXTURES["blocks"], ids=lambda c: c["name"])
def test_blocks(case):
    result = parse_block(expand(case))
    actual = {"reason": result.reason} if result.reason is not None else {"spec": spec_to_json(result.spec)}
    assert actual == case["result"]


@pytest.mark.parametrize("case", FIXTURES["sections"], ids=lambda c: c["name"])
def test_sections(case):
    assert segments_to_json(parse_section(expand(case), case["section"])) == case["segments"]


@pytest.mark.parametrize("case", FIXTURES["tickets"], ids=lambda c: c["name"])
def test_tickets(case):
    labels = case.get("labels")
    label = (lambda s: labels.get(s, s)) if labels is not None else None
    assert (
        segments_to_json(resolve_ticket_widgets(case["body"], order=case.get("order"), label=label)) == case["result"]
    )
