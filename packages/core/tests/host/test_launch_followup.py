from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from orch.host import agents
from orch.host.errors import HostError
from orch.host.launch import MODE_TEXT, MODES, launch_argv, prompt
from orch.runner.config import RunnerConfig
from orch.runner.link import registry_argv

FU = "fu_" + "0" * 26


def test_fixed_prompt_and_registry_argv():
    expected = (
        f"Run orch status. You were started by follow-up {FU} on ticket ABC-1. "
        f"Run orch follow-up show {FU}, then orch show ABC-1, then claim the ticket and continue the work. "
        "Follow rules.md."
    )
    task = {"followup": FU, "brief": "untrusted text"}
    assert prompt("followup", "ABC-1", "rules.md", task) == expected
    assert "followup" in MODES and "followup" not in MODE_TEXT
    for harness in ("codex", "claude-code"):
        argv, _, _ = launch_argv(harness, "followup", None, "ABC-1", RunnerConfig(), "rules.md", task=task)
        assert argv[-1] == expected and argv.count(expected) == 1
        assert registry_argv(argv, {"SHELL": "/bin/sh"}, RunnerConfig())[0] == "/bin/sh"


@pytest.mark.parametrize("fu", [None, "", "fu_" + "8" * 26, FU + "\n", FU + "; echo bad", 123])
def test_bad_id(fu):
    with pytest.raises(HostError) as exc:
        prompt("followup", "ABC-1", task={"followup": fu})
    assert exc.value.code == "launch.invalid"


def test_plan_followup_and_ordinary_plan(monkeypatch, tmp_path):
    from orch.runner import config

    service = NS(workspace_id="a" * 32, entry=NS(root=tmp_path), host_uid=1, store=object())
    call = NS(service=service, viewer="p_" + "b" * 32)
    hub = NS(host=NS(state_dir=tmp_path), connected=True, require_current=lambda: None, grants=Mock())
    view = NS(key="ABC-1", claim=None)
    monkeypatch.setattr(agents.Viewer, "of", lambda _: NS(ticket=lambda *_: view))
    monkeypatch.setattr(agents, "agent_missing", lambda *_: None)
    monkeypatch.setattr(agents, "_shell_ok", lambda: True)
    monkeypatch.setattr(config, "read_config", lambda *_: RunnerConfig())
    monkeypatch.setattr(
        agents,
        "_cwd",
        lambda *a, **kw: dict(
            cwd={"kind": "workspace"}, path=str(tmp_path), pin={}, create=None, mkdir=False, other={}
        ),
    )
    args = dict(ticket="ABC-1", harness="codex", mode="followup", model="o3")
    fu = dict(id=FU, grant="gr_" + "0" * 26, until="2030-01-01T00:00:00Z", scope="ticket-uid", person=call.viewer)
    out = agents.plan(call, args, hub, agent=True, fu=fu)
    assert out["blocked"] is None
    assert out["_launch"]["run_grant"] is True
    assert out["grant"] == dict(id=fu["grant"], until=fu["until"], scope=fu["scope"])
    hub.grants.current.assert_not_called()
    hub.grants.launch_reason.assert_not_called()
    assert agents.plan(call, args, hub, agent=True, fu={**fu, "id": "fu_" + "1" * 26})["spec"] != out["spec"]
    assert agents.plan(call, {**args, "mode": "work"}, hub, agent=True, fu=fu)["blocked"]["code"] == "launch.invalid"
    hub.grants.current.return_value = dict(grant=fu["grant"], until=fu["until"], scope="workable")
    hub.grants.launch_reason.return_value = None
    ordinary = agents.plan(call, {**args, "mode": "work"}, hub, agent=True)
    assert ordinary == agents.plan(call, {**args, "mode": "work"}, hub, agent=True, fu=None)
    assert ordinary["blocked"] is None and "run_grant" not in ordinary["_launch"]
    assert "followup" not in agents._PREVIEW["mode"]["enum"]
    # Mode followup without the host's own follow-up or schedule is refused, whatever a caller sends.
    for task_args in ({}, {"brief": f"sc_{'0' * 26}", "label": "sc"}):
        blocked = agents.plan(call, {**args, **task_args}, hub, agent=True)["blocked"]
        assert blocked["code"] == "launch.invalid"
    sc = {**fu, "id": SC}
    assert agents.plan(call, args, hub, agent=False, fu=sc)["blocked"]["code"] == "launch.invalid"
    assert agents.plan(call, args, hub, agent=True, fu=sc)["blocked"] is None


SC = "sc_" + "0" * 26


def test_schedule_prompt_and_only_the_host_reaches_mode_followup():
    from orch.host import terms

    text = prompt("followup", "ABC-1", task={"followup": SC})
    assert f"started by schedule {SC} on ticket ABC-1" in text
    for op in ("agent.preview", "agent.start"):
        body = {"ticket": "ABC-1", "harness": "codex", "mode": "followup", "spec": "0" * 64}
        if op == "agent.preview":
            body.pop("spec")
        with pytest.raises(HostError) as exc:
            agents._validate(op, body)
        assert exc.value.code == "invalid.input"
    assert terms._PREVIEW["mode"]["enum"] == ["context"]
    for bad in ("sc_" + "8" * 26, SC + "\n", SC + " x", "sc_" + "0" * 25):
        with pytest.raises(HostError):
            prompt("followup", "ABC-1", task={"followup": bad})
