"""D2: the launch belongs to a complete signed sequence, never to browser arguments."""

from types import SimpleNamespace

import pytest

from orch.click import view
from orch.click.protocol import DEPOSIT_RISK
from orch.host import agents, sequence
from orch.host.errors import HostError
from tests.host import human_helpers
from tests.host.test_sequence import Rig, intent_of

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def launch_intent():
    return intent_of(launch={"harness": "codex", "mode": "refine"}, agent={"hours": 8, "scope": "workable"})


def test_partial_sequence_cannot_start_and_complete_uses_host_values(human, tmp_path):
    rig = Rig(human, tmp_path, launch_intent())
    first = rig.first()
    rid = first["request"]["id"]
    service = human.served.service
    record = service.requests.read(rid)
    call = SimpleNamespace(service=service, viewer=rig.person, principal=record["principal"])
    # Simulate death after the ticket was created: the driver can only replay this signature on resume.
    real = rig.port.submit

    def stop_after_create(*args):
        real(*args)
        raise OSError("signer killed")

    rig.port.submit = stop_after_create
    with pytest.raises(sequence.Aborted):
        rig.driver().run(first)
    assert service.store.ticket(first["material"]["ticket"]["key"]) is not None
    with pytest.raises(HostError, match="every signature") as caught:
        agents.after_sequence(call, rid)
    assert caught.value.code == "sequence.incomplete"
    rig.port.submit = real
    rig.driver().run(None)
    args, idem = agents.after_sequence(call, rid)
    assert args == {
        "ticket": first["material"]["ticket"]["key"],
        "harness": "codex",
        "mode": "refine",
        "worktree": True,
    }
    assert idem == "a_" + rid[3:]
    assert agents.after_sequence(call, rid) == (args, idem)
    call.viewer = "p_" + "0" * 32
    with pytest.raises(HostError) as caught:
        agents.after_sequence(call, rid)
    assert caught.value.code == "not_found"


def test_sequence_view_marks_last_grant_and_launch(human, tmp_path):
    rig = Rig(human, tmp_path, launch_intent())
    first = rig.first()
    got = view.build(
        first,
        lambda _: ("Workspace", "DEMO"),
        device=human.person.device,
        expires_at=first["request"]["expires_at"],
        artifacts=None,
    )
    assert got["headline"] == "Create ticket and start agent"
    assert DEPOSIT_RISK in got["risk_lines"]
    assert "Agents can run as you for 8 hours" in got["risk_lines"]
    assert any("Then: start an agent on the new ticket." in line for line in got["lines"])
    assert sequence.steps(rig.intent)[-1]["op"] == "grant"


def test_start_ignores_browser_ticket_spec_and_idem(human, tmp_path, monkeypatch):
    rig = Rig(human, tmp_path, launch_intent())
    first = rig.first()
    rig.driver().run(first)
    service = human.served.service
    rid = first["request"]["id"]
    record = service.requests.read(rid)
    hub = SimpleNamespace(store=SimpleNamespace(records={}))
    monkeypatch.setattr(service, "runners", hub)
    calls = []

    def plan(call, args, hub, agent):
        calls.append(args)
        return {"blocked": None, "spec": "a" * 64, "_launch": {}, "model": None, "cwd": {}, "policy": {}}

    monkeypatch.setattr(agents, "plan", plan)
    call = SimpleNamespace(
        service=service,
        viewer=rig.person,
        principal=record["principal"],
        frame={
            "op": "agent.start",
            "args": {"after_request": rid, "ticket": "EVIL-0001", "harness": "claude-code", "spec": "b" * 64},
            "opts": {"idem": "i_browser"},
        },
    )
    data = agents.serve(call)["data"]
    assert calls == [
        {"ticket": first["material"]["ticket"]["key"], "harness": "codex", "mode": "refine", "worktree": True}
    ]
    assert data["run"] == "r_" + rid[3:]
    hub.store.records[data["run"]] = {
        "workspace_id": service.workspace_id,
        "person": rig.person,
        "kind": "agent",
        "spec": data["spec"],
        "after_request": rid,
    }
    assert agents.serve(call)["data"] == {"replay": data["run"]}
    assert len(calls) == 1


def test_valid_access_omits_grant_and_only_info_mentions_access(human, tmp_path, monkeypatch):
    from orch.host import runnergrants

    monkeypatch.setattr(runnergrants, "reusable", lambda *args: {"until": "2026-10-28T18:00:00Z"})
    intent = launch_intent()
    del intent["agent"]
    rig = Rig(human, tmp_path, intent)
    first = rig.first()
    human.check(first)
    got = view.build(
        first,
        lambda _: ("Workspace", "DEMO"),
        device=human.person.device,
        expires_at=first["request"]["expires_at"],
        artifacts=None,
    )
    assert got["risk_lines"] == []
    assert "grant" not in [s["op"] for s in sequence.steps(rig.intent)]
    assert any("Uses your current agent access until" in line for line in got["info_lines"])
    assert not any("Uses your current agent access" in line for line in got["lines"])
    assert "Uses your current agent access" not in str(got["content"])


@pytest.mark.parametrize("left,valid", [(899, False), (900, True), (901, True)])
def test_renewal_threshold_uses_host_authority(left, valid):
    from orch.host.runnergrants import reusable

    cur = {"grant": "gr_test"}
    grants = SimpleNamespace(
        current=lambda *args: cur,
        launch_reason=lambda *args: None,
        valid=lambda *args, **kw: None if left >= kw["left"] else "expiring",
    )
    service = SimpleNamespace(runners=SimpleNamespace(grants=grants), workspace_id="test")
    assert (reusable(service, "person") is not None) is valid


def test_click_retry_resumes_same_sequence_without_another_ticket(served, monkeypatch):
    from orch.click.protocol import ClickError
    from orch.host import runnergrants
    from tests.host.test_click import Click
    from tests.host.test_sequence_quick import click_prepare

    monkeypatch.setattr(runnergrants, "reusable", lambda *args: {"until": "2026-10-28T18:00:00Z"})
    intent = launch_intent()
    del intent["agent"]
    click = Click(served)
    try:
        prepared = click_prepare(click, intent)
        shown = click.view(prepared["request_id"])
        click.mono.t += 2
        once = []

        def lose_first_answer(op, args, result):
            if op == "human.submit" and not once:
                once.append(True)
                raise OSError("lost answer after landing")
            return result

        click.tamper = lose_first_answer
        with pytest.raises(ClickError) as caught:
            click.sign(prepared["request_id"], shown["view_digest"])
        assert caught.value.reason == "resumable"
        click.tamper = None
        result = click.sign(prepared["request_id"], shown["view_digest"])
        assert result["result"]["key"]
        assert click.signatures == len(sequence.steps(intent))
    finally:
        click.close()


def test_host_refuses_redundant_grant_before_creating_ticket(human, tmp_path, monkeypatch):
    from orch.host import runnergrants

    monkeypatch.setattr(runnergrants, "reusable", lambda *args: {"until": "2026-10-28T18:00:00Z"})
    rig = Rig(human, tmp_path, launch_intent())
    op, args = sequence.step_args(rig.intent, 0, None)
    res = human.c.req("human.prepare", {"op": op, "args": args, "signer": human.signer, "sequence": rig.meta0()})
    assert res["error"]["code"] == "sequence.changed"


@pytest.mark.parametrize("model", ["model\nforged review", " model", "x\u202e", {"name": "codex"}])
def test_launch_model_cannot_inject_review_lines(model):
    intent = launch_intent()
    intent["launch"]["model"] = model
    with pytest.raises(sequence.SequenceInvalid):
        sequence.validate_intent(intent)


def test_daily_op_cannot_hide_grant_in_risk_table():
    from orch.click.protocol import ONE_PRESS_OPS, risk_lines

    assert "grant" not in ONE_PRESS_OPS
    for op in ONE_PRESS_OPS:
        lines = risk_lines(op, [{"type": "grant.issued", "hours": 8}], deposit=True)
        assert "Agents can run as you for 8 hours" in lines
