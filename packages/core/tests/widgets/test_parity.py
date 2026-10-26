"""Both runtimes consume the dashboard-authored results in the same fixture file."""

import json
import os
from pathlib import Path

import pytest

from orch.widgets import clip_reason, parse_block, parse_section, resolve_ticket_widgets, segments_to_json, spec_to_json

PATH = Path(__file__).resolve().parents[4] / "packages/addons/dashboard/src/app/pages/ticket/widgets/parity-fixtures.json"
if not PATH.exists():
    if os.environ.get("CI"):
        raise RuntimeError(f"Shared widget parity fixtures are required in CI: {PATH}")
    pytest.skip(f"Shared dashboard widget parity fixtures are absent: {PATH}", allow_module_level=True)
FIXTURES = json.loads(PATH.read_text())


def clipped(v):
    """The expected results as Python shows them: reasons clipped (the TS reasons stay full)."""
    if isinstance(v, dict):
        return {
            k: x if k == "spec" else clip_reason(x) if k == "reason" and isinstance(x, str) else clipped(x)
            for k, x in v.items()
        }
    if isinstance(v, list):
        return [clipped(x) for x in v]
    return v


def expand(case):
    if "gen" in case:
        g = case["gen"]
        return g["prefix"] + g["unit"] * g["count"] + g["suffix"]
    return case.get("raw", case.get("text", ""))


@pytest.mark.parametrize("case", FIXTURES["blocks"], ids=lambda c: c["name"])
def test_blocks(case):
    result = parse_block(expand(case))
    actual = {"reason": result.reason} if result.reason is not None else {"spec": spec_to_json(result.spec)}
    assert actual == clipped(case["result"])


@pytest.mark.parametrize("case", FIXTURES["sections"], ids=lambda c: c["name"])
def test_sections(case):
    assert segments_to_json(parse_section(expand(case), case["section"])) == clipped(case["segments"])


@pytest.mark.parametrize("case", FIXTURES["tickets"], ids=lambda c: c["name"])
def test_tickets(case):
    labels = case.get("labels")
    label = (lambda s: labels.get(s, s)) if labels is not None else None
    assert segments_to_json(resolve_ticket_widgets(case["body"], order=case.get("order"), label=label)) == clipped(
        case["result"]
    )
