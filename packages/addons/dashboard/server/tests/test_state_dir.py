"""The dashboard finds the state directory exactly where core does (orch.ops.runtime.state_dir), for every
combination of ORCH_STATE_DIR, XDG_CONFIG_HOME and HOME; otherwise the dashboard and the host look in different
places."""

import pytest
from orch.ops.runtime import state_dir as core_state_dir

from orch_dashboard.gateway import state_dir

CASES = [
    {"HOME": "/home/u"},
    {"HOME": "/home/u", "XDG_CONFIG_HOME": "/xdg"},
    {"HOME": "/home/u", "XDG_CONFIG_HOME": "relative/xdg"},  # invalid per the XDG spec: ignored
    {"HOME": "/home/u", "XDG_CONFIG_HOME": ""},
    {"HOME": "/home/u", "XDG_CONFIG_HOME": "/xdg", "ORCH_STATE_DIR": "/state"},
    {"HOME": "/home/u", "ORCH_STATE_DIR": "relative/state"},  # made absolute against the cwd
    {"HOME": "/home/u", "ORCH_STATE_DIR": ""},  # empty is unset
]


@pytest.mark.parametrize("env", CASES, ids=lambda e: ",".join(f"{k}={v}" for k, v in sorted(e.items())))
def test_the_dashboard_resolves_the_state_dir_like_core(env):
    assert state_dir(env) == core_state_dir(env)


def test_xdg_config_home_is_honoured():
    assert str(state_dir({"HOME": "/home/u", "XDG_CONFIG_HOME": "/xdg"})) == "/xdg/orch"
