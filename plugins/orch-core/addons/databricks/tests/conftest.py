import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the addon folder, so tests import orch_databricks

from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401


@pytest.fixture(autouse=True)
def home(orch_user_dir, tmp_path, monkeypatch):
    """A fake home: ~/.databrickscfg and the CLI token cache are never the real ones."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path
