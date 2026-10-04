import json
import sys
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADDON))  # tests import github_issues

from orch.testing import FakeRunner, fake_workspace  # noqa: E402
from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401

FIXTURES = ADDON / "tests" / "fixtures"
GH = {"prefix": "GH", "pattern": "GH-(?P<id>\\d+)", "url": "https://github.com/acme/ticket-orch-demo/issues/{id}"}
SCOPE = "acme/ticket-orch-demo"
TICKETS = [
    {"title": "Retry gateway downloads", "status": "testing", "external": ["GH-1"]},           # DEMO-0001, in sync
    {"title": "Consistent timestamp formats", "status": "in-progress", "external": ["GH-13"]},  # DEMO-0002, closed there
    {"title": "Quality report divides by zero", "status": "done", "external": ["GH-5"]},       # DEMO-0003, open there
    {"title": "Partition mart by month", "status": "open", "external": ["GH-6"]},              # DEMO-0004, in sync
]


@pytest.fixture(autouse=True)
def _user_dir(orch_user_dir):
    return orch_user_dir


def recordings() -> list[dict]:
    out = []
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.extend(data if isinstance(data, list) else [data])
    return out


def runner(*first, strict=True) -> FakeRunner:
    return FakeRunner([*first, *recordings()], strict=strict)


@pytest.fixture
def issues_ws(tmp_path):
    return fake_workspace(tmp_path / "demo", prefix="DEMO", trackers=[GH], tickets=TICKETS)
