"""D2: the launch belongs to a complete signed sequence, never to browser arguments."""

from types import SimpleNamespace

import pytest

from orch.click import view
from orch.click.protocol import DEPOSIT_RISK, ONE_PRESS_OPS
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
    assert got["op"] == "grant" and got["op"] not in ONE_PRESS_OPS
    assert got["confirm"] == {"kind": "click", "mode": "press"}
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
    assert got["op"] in ONE_PRESS_OPS
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
        submissions = []

        def lose_first_answer(op, args, result):
            if op == "human.submit":
                from orch import canon

                submissions.append((args["request_id"], canon.dumps(args["events"]), result))
                if len(submissions) == 1:
                    raise OSError("lost answer after landing")
            return result

        click.tamper = lose_first_answer
        with pytest.raises(ClickError) as caught:
            click.sign(prepared["request_id"], shown["view_digest"])
        assert caught.value.reason == "resumable"
        result = click.sign(prepared["request_id"], shown["view_digest"])
        assert result["result"]["key"]
        assert click.signatures == len(sequence.steps(intent))
        assert submissions[0][:2] == submissions[1][:2]
        assert submissions[1][2]["duplicate"] is True
        from tests.host.human_helpers import ok

        rows = ok(served.client().req("ticket.query", {}))["data"]
        tickets = next(value for value in rows.values() if isinstance(value, list))
        assert len([ticket for ticket in tickets if ticket["title"] == intent["title"]]) == 1
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


@pytest.mark.parametrize("at_sign", [False, True])
def test_launch_grant_requires_execution_at_prepare_and_sign(served, at_sign):
    from tests.host.test_click import Click, refused
    from tests.host.test_sequence_quick import click_prepare

    click = Click(served)
    try:
        click.signer._execution = lambda entry: at_sign
        if not at_sign:
            refused(lambda: click_prepare(click, launch_intent()), "human.click_refused", "terminal_only")
        else:
            prepared = click_prepare(click, launch_intent())
            shown = click.view(prepared["request_id"])
            click.mono.t += 2
            click.signer._execution = lambda entry: False
            refused(
                lambda: click.sign(prepared["request_id"], shown["view_digest"]), "human.click_refused", "terminal_only"
            )
        assert click.signatures == 0
    finally:
        click.close()


def test_sequence_risk_uses_the_v2_grant_payload(human, tmp_path, monkeypatch):
    rig = Rig(human, tmp_path, launch_intent())
    first = rig.first()
    real = view.risk_lines
    seen = []

    def risk(op, events=None, **kwargs):
        if op == "grant":
            seen.extend(events)
        return real(op, events, **kwargs)

    monkeypatch.setattr(view, "risk_lines", risk)
    shown = view.build(
        first,
        lambda _: ("Workspace", "DEMO"),
        device=human.person.device,
        expires_at=first["request"]["expires_at"],
        artifacts=None,
    )
    grant = next(e for e in seen if e["type"] == "grant.issued")
    rig.driver().run(first)
    signed = next(e for _, events, _ in rig.submitted for e in events if e["type"] == "grant.issued")
    assert all(signed[k] == v for k, v in grant.items() if k != "secret_hash")
    assert shown["risk_lines"] == real("grant", [signed], deposit=True)


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "new"},
        {"risk_lines": []},
        {"confirm": {"kind": "click", "mode": "arm"}},
    ],
)
def test_core_requires_grant_risk_and_press_view(served, monkeypatch, bad):
    from tests.host.test_click import Click, refused
    from tests.host.test_sequence_quick import click_prepare

    click = Click(served)
    click.signer._execution = lambda entry: True
    real = view.build
    try:
        prepared = click_prepare(click, launch_intent())
        monkeypatch.setattr(view, "build", lambda *args, **kwargs: {**real(*args, **kwargs), **bad})
        refused(lambda: click.view(prepared["request_id"]), "human.click_refused", "check_failed")
        assert click.signatures == 0
    finally:
        click.close()


@pytest.mark.parametrize("status", ["revoked", "removed"])
def test_launch_submit_without_signer_get_refuses_invalid_device(human, tmp_path, status):
    from tests.host.human_helpers import err, ok

    rig = Rig(human, tmp_path, launch_intent())
    prepared = rig.first()
    rid = prepared["request"]["id"]
    # A read without a signer does not unbind the prepare-time device/auth.
    read = ok(human.c.req("human.get", {"request_id": rid}))["data"]
    assert read["events"] == prepared["events"]
    signed = human.sign(read)
    ws = human.served.lab.ws
    ws.tick()
    store = ws.other()
    ws.store = store
    try:
        ws.add_device()  # keep a working device so the person connection remains authorized
        if status == "revoked":
            ws.revoke_device(human.person.device)
        else:
            store.append(
                ws.person_event(ws.owner, "workspace", "device.removed", device=human.person.device),
                log="workspace",
            )
    finally:
        store.close()
        ws.store = None
    assert err(human.submit_res(rid, signed.events)) == "device.invalid"
    rows = ok(human.c.req("ticket.query", {}))["data"]
    assert not next(value for value in rows.values() if isinstance(value, list))
