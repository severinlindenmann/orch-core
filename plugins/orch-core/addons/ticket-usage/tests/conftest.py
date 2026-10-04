import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the addon folder, so tests import orch_ticket_usage

from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _user_dir(orch_user_dir):
    return orch_user_dir


@pytest.fixture(autouse=True)
def _no_real_claude_files(tmp_path, monkeypatch):
    """The addon reads Claude's dir and a limits log under HOME by default: point both at an empty temp dir."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "home" / ".claude"))


@pytest.fixture(autouse=True)
def _utc(monkeypatch):
    """Days are the machine's local days: pin the zone so the expected days do not depend on where tests run."""
    import time
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()
