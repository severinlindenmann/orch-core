import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # the addon folder, so tests import model_routing

from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: E402,F401


@pytest.fixture(autouse=True)
def _user_dir(orch_user_dir):
    return orch_user_dir


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("CLAUDE_CODE_SUBAGENT_MODEL_FORCE", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        monkeypatch.delenv(v, raising=False)
