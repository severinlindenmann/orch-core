"""Direct runtime opens cannot establish trust on behalf of an agent."""

import pytest

from orch.ops.caps import Files
from orch.ops.errors import OrchError
from orch.ops.runtime import Workspace


@pytest.mark.parametrize("marker", ["ORCH_GRANT", "ORCH_TICKET", "ORCH_WORKDIR"])
@pytest.mark.parametrize("value", ["present", ""])
def test_direct_open_never_creates_an_agent_genesis_pin(env, tmp_path, marker, value):
    env.bootstrap()
    state = tmp_path / "fresh-state"
    process_env = {"ORCH_WORKSPACE": str(env.root), "ORCH_STATE_DIR": str(state)}
    pin = state / "hosts" / env.ws_id / "genesis"
    agent = Workspace(process_env | {marker: value}, files=Files.direct())
    try:
        with pytest.raises(OrchError) as caught:
            _ = agent.store
        assert caught.value.code == "host.unavailable"
        assert "an agent never pins a workspace" in caught.value.message
        assert not pin.exists()
        assert not state.exists()
        assert not agent.pin_created
    finally:
        agent.close()
    person = Workspace(process_env, files=Files.direct())
    try:
        assert person.store is not None
        assert person.pin_created
        before = pin.read_bytes()
    finally:
        person.close()
    try:
        assert agent.store is not None
        assert not agent.pin_created
        assert pin.read_bytes() == before
    finally:
        agent.close()


def test_a_persons_own_session_still_pins_on_first_use(env, tmp_path):
    """ORCH_SESSION alone (a person's Claude Code session with the orch plugin) is no agent marker."""
    env.bootstrap()
    state = tmp_path / "fresh-state"
    person = Workspace(
        {"ORCH_WORKSPACE": str(env.root), "ORCH_STATE_DIR": str(state), "ORCH_SESSION": "s_" + "0" * 26},
        files=Files.direct(),
    )
    try:
        assert person.store is not None and person.pin_created
    finally:
        person.close()
