import json
import sys
from pathlib import Path

import pytest

ADDON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADDON))  # tests import github_reviews

from orch.testing import FakeRunner, fake_workspace  # noqa: E402
from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401

FIXTURES = ADDON / "tests" / "fixtures"
INGEST_URL = "https://github.com/acme/ticket-orch-demo-ingest.git\n"
DEMO_TICKETS = [
    {"title": "Retry gateway downloads", "status": "testing",
     "meta": {"prs": [{"repo": "acme-energy-data", "url": "https://github.com/acme/ticket-orch-demo/pull/19", "state": "draft"}]}},
    {"title": "Vary suspect threshold", "status": "in-progress"},
    {"title": "Guard suspect-rate summary", "status": "in-progress"},
    {"title": "Coverage reporting", "status": "in-progress"},
    {"title": "Pin dependency upper bounds", "status": "waiting"},
    {"title": "Skip malformed rows", "status": "done"},
    {"title": "Accept dd.mm.yyyy timestamps", "status": "done"},
    {"title": "Document the dbt spike", "status": "done"},
    {"title": "Reject late exports", "status": "in-progress", "meta": {"blocked_by": ["DEMO-0003"]}},
]


@pytest.fixture(autouse=True)
def _user_dir(orch_user_dir):
    """Never read the real ~/.config/orch (A1 F13)."""
    return orch_user_dir


def recordings() -> list[dict]:
    out = []
    for path in sorted(FIXTURES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.extend(data if isinstance(data, list) else [data])
    return out


def demo_runner(fw, *first, strict=True) -> FakeRunner:
    """The recorded fixtures, after `first` (recordings that must win) and the ingest repo's own remote."""
    ingest = str((fw.ws.root / "ingest").resolve())
    own = {"argv": ["git", "-C", ingest, "remote", "get-url", "origin"], "stdout": INGEST_URL}
    return FakeRunner([*first, own, *recordings()], strict=strict)


@pytest.fixture
def demo(tmp_path):
    """The demo harness (git.repos `acme-energy-data` at ".") plus a sub-repo `ingest`, DEMO-0001 … DEMO-0009."""
    return fake_workspace(tmp_path / "demo", prefix="DEMO",
                          repos={"acme-energy-data": {"path": "."}, "ingest": {"path": "ingest"}}, tickets=DEMO_TICKETS)
