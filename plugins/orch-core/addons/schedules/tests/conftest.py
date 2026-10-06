import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the addon folder, so tests import orch_schedules

from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _user_dir(orch_user_dir):
    return orch_user_dir
