"""B8: the grant ceiling (168 h) is one constant; the dashboard and schema copies are cross-checked."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from orch.model.types import GRANT_MAX_HOURS

ROOT = Path(__file__).resolve().parents[4]
SCHEMAS = Path(__file__).resolve().parents[2] / "src" / "orch" / "schema" / "schemas"


def test_the_ceiling_is_seven_days():
    assert GRANT_MAX_HOURS == 168


def test_the_schemas_agree():
    ev = json.loads((SCHEMAS / "event.grant.issued.json").read_text())
    st = json.loads((SCHEMAS / "event.settings.changed.json").read_text())
    ws = json.loads((SCHEMAS / "workspace.json").read_text())
    assert ev["properties"]["hours"]["maximum"] == GRANT_MAX_HOURS
    assert json.dumps(st).count(f'"maximum": {GRANT_MAX_HOURS}') >= 1
    assert json.dumps(ws).count(f'"maximum": {GRANT_MAX_HOURS}') >= 1


@pytest.mark.parametrize(
    ("rel", "pattern"),
    [
        ("packages/addons/dashboard/devhost/settingsops.py", r'"grant_hours": \(1, (\d+)\)'),
        ("packages/addons/dashboard/server/src/orch_dashboard/adapters/settings.py", r'"grant_hours": \(1, (\d+)\)'),
        (
            "packages/addons/dashboard/server/src/orch_dashboard/handoff.py",
            r"grant_hours: Annotated\[int, Field\(ge=1, le=(\d+)\)",
        ),
        (
            "packages/addons/dashboard/server/src/orch_dashboard/handoff.py",
            r"hours: Annotated\[int, Field\(ge=1, le=(\d+)\)\] = 8",
        ),
        ("packages/addons/dashboard/src/api/grants.ts", r"GRANT_MAX_HOURS = (\d+)"),
    ],
)
def test_the_dashboard_copies_agree(rel, pattern):
    path = ROOT / rel
    if not path.exists():
        pytest.skip("not in the monorepo")
    m = re.search(pattern, path.read_text())
    assert m is not None and int(m.group(1)) == GRANT_MAX_HOURS, rel
