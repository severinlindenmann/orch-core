"""The AI Factory switch is signed (`orch factory on`), never the config value alone: an agent can edit the config."""
import json

import pytest

from orch import actor
from orch.cli import run
from orch.core import factory_runner, permits
from orch.errors import HumanOnlyError, UsageError
from test_factory import SESSION, _behavior, _payload, _refine, bind


@pytest.fixture
def unsigned(ws_root):
    """What an agent's edit of orchestrator/config.json does: factory.enabled true, nothing signed."""
    from conftest import make_config
    from orch.core.workspace import Workspace
    (ws_root / "orchestrator" / "config.json").write_text(json.dumps(make_config(factory={"enabled": True})))
    return Workspace.open(ws_root)


def test_the_config_alone_switches_nothing_on(unsigned, human):
    from conftest import human_ops
    from orch.core.ops import Ops
    assert permits.config_enabled(unsigned) and not permits.enabled(unsigned)
    assert permits.off_reason(unsigned) == permits.UNSIGNED and "orch factory on" in permits.UNSIGNED
    assert factory_runner.runner_blocker(unsigned) == permits.UNSIGNED
    e = Ops(unsigned, human).new("Billing", type="epic")
    _refine(Ops(unsigned, human), e.id, plan=None)
    with pytest.raises(UsageError, match="not signed"):
        human_ops(unsigned, human).approve(e.id, "requirements", delegate={"factory": True})


def test_only_the_human_signs_it_on_and_anyone_off(unsigned, human, agent):
    from orch.core.ops import Ops
    with pytest.raises(HumanOnlyError):
        Ops(unsigned, agent).set_factory(True)
    assert not permits.enabled(unsigned)
    Ops(unsigned, human).set_factory(True)
    assert permits.enabled(unsigned)
    assert json.loads((unsigned.home / "config.json").read_text())["factory"]["enabled"] is True
    Ops(unsigned, agent).set_factory(False)  # the brake: anyone
    assert not permits.enabled(unsigned) and permits.off_reason(unsigned) != permits.UNSIGNED
    unsigned.config["factory"]["enabled"] = True  # editing the config back on does not undo a signed off
    assert not permits.enabled(unsigned)


def test_a_bound_session_is_denied_when_the_switch_is_not_signed(configure, human, agent):
    from conftest import human_ops, sign_factory
    from orch.core.ops import Ops
    ws = configure(factory={"enabled": True})
    fa, fh = Ops(ws, agent), human_ops(ws, human)
    e = fa.new("Billing", type="epic")
    _refine(fa, e.id, plan=None)
    fh.approve(e.id, "requirements", delegate={"factory": True})
    c = fa.new("child", epic=e.id)
    _refine(fa, c.id)
    fa.epic_auto_approve(c.id)
    bind(ws, human, e.id, c.id)
    sign_factory(ws, on=False)
    out = permits.hook_decision(ws, _payload("make e2e", session=SESSION))
    assert _behavior(out) == "deny"  # never left to the harness: the runner launched it
    assert permits.hook_decision(ws, _payload("make e2e", session="99999999-2222-4333-8444-555555555555")) is None


def test_orch_factory_status_names_the_migration(unsigned, capsys, monkeypatch):
    monkeypatch.setenv("ORCH_HARNESS", "test-agent")
    monkeypatch.setattr(actor, "is_interactive", lambda: False)
    assert run(["factory", "status"]) == 0
    assert "orch factory on" in capsys.readouterr().out
    assert run(["factory", "on"]) != 0  # an agent: refused
    assert not permits.enabled(unsigned)
