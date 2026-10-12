"""The dashboard resolves its state directory exactly as core does (the click signer child must read the same one)."""

from pathlib import Path

import pytest
from orch.ops.runtime import state_dir as core

from orch_dashboard.gateway import state_dir


@pytest.mark.parametrize(
    "env",
    [
        {"ORCH_STATE_DIR": "/tmp/x/state", "HOME": "/h", "XDG_CONFIG_HOME": "/xdg"},
        {"XDG_CONFIG_HOME": "/xdg", "HOME": "/h"},
        {"XDG_CONFIG_HOME": "relative/xdg", "HOME": "/h"},
        {"HOME": "/h"},
    ],
)
def test_the_same_order_as_core(monkeypatch, env):
    for name in ("ORCH_STATE_DIR", "XDG_CONFIG_HOME", "HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert state_dir() == core(env) and isinstance(state_dir(), Path)
