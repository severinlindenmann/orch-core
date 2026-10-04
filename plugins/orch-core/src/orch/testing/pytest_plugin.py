"""Fixtures for addon tests. In your tests' conftest.py:
    from orch.testing.pytest_plugin import orch_user_dir, orch_workspace  # noqa: F401
"""
import pytest

from orch.testing.workspace import fake_workspace

_AGENT_ENV = ("ORCH_HOME", "ORCH_HARNESS", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ORCH_SESSION", "ORCH_MODEL",
              "CLAUDE_CODE_ENTRYPOINT", "AI_AGENT", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "GEMINI_CLI")


@pytest.fixture
def orch_user_dir(tmp_path, monkeypatch):
    d = tmp_path / "orch-user"
    monkeypatch.setenv("ORCH_STATE_DIR", str(d))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in _AGENT_ENV:
        monkeypatch.delenv(var, raising=False)
    # Tests may run under an agent harness or CI: human actions in a test must not depend on the real process tree.
    import orch.actor
    monkeypatch.setattr(orch.actor, "process_chain", lambda: [])
    return d


@pytest.fixture
def orch_workspace(tmp_path, orch_user_dir):
    return fake_workspace(tmp_path / "workspace", tickets=[{"title": "Example ticket", "status": "open"}])
