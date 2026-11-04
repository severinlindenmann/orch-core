"""Follow-ups (#720): the read route over the host's `follow_up.list`, and the one signed hand-off (Cancel)."""

import pytest
from conftest import ORIGIN

from orch_dashboard import followups
from orch_dashboard.signing import FollowupCancel, FollowupReschedule, FollowupUndo, expected, host_call

FU = "fu_" + "0" * 26


def headers(token):
    return {"Authorization": f"Bearer {token}", "Origin": ORIGIN}


@pytest.fixture
def fu_host(gateway, stub, monkeypatch):
    link = gateway.enabled(stub.ws)
    link.welcome["ops"]["follow_up.list"] = 1
    original = link.call
    script = {"calls": [], "on": True, "rows": []}

    def call(op, args=None, **kwargs):
        if op == "follow_up.list":
            script["calls"].append(args)
            return {"rows": script["rows"], "total": len(script["rows"])}
        if op == "addon.list":
            rows = original("addon.list")["addons"]
            return {"addons": [*rows, {"name": "schedules", "state": "active" if script["on"] else "off"}]}
        return original(op, args, **kwargs)

    monkeypatch.setattr(link, "call", call)
    return script


def test_rows_are_drawn_and_filtered(client, token, stub, fu_host):
    key = "DEMO-0001"
    fu_host["rows"] = [
        {
            "id": FU,
            "key": key,
            "state": "pending",
            "trigger": "time",
            "due": "2026-10-28T09:00:00Z",
            "brief": "Check it.\n\n- a\n- b",
            "person": stub.person,
            "set_at": "2026-10-27T09:00:00Z",
            "fired_at": None,
            "late_by": None,
            "reason": None,
        },
        {"id": "fu_x", "key": key, "state": "weird"},
        {
            "id": FU[:-1] + "1",
            "key": key,
            "state": "did_not_run",
            "outcome": "did_not_run",
            "reason": "no_host",
            "brief": "a‮b",
        },
    ]
    got = client.get(f"/api/w/{stub.ws}/followups", headers=headers(token))
    body = got.json()
    assert body["available"] is True and body["can"] == {"cancel": True, "reschedule": False, "undo": False}
    assert [r["state"] for r in body["rows"]] == ["pending", "did_not_run"]
    first, second = body["rows"]
    assert first["brief"] == "Check it.\n\n- a\n- b" and first["mine"] is True and first["can_cancel"] is True
    assert second["brief"] is None and second["brief_hidden"] is True and second["reason"] == "no_host"
    assert {"all": True} in fu_host["calls"]
    assert "schedules" in client.get("/api/session", headers=headers(token)).json()["features"]


def test_off_means_nothing(client, token, stub, fu_host):
    fu_host["on"] = False
    got = client.get(f"/api/w/{stub.ws}/followups", headers=headers(token))
    assert got.status_code == 409 and got.json()["error"]["code"] == "addon.disabled"
    assert fu_host["calls"] == []


def test_unreasoned_reason_is_dropped():
    r = followups.row({"id": FU, "state": "failed", "reason": "<script>", "key": "A-1"}, {}, "p_1", "member")
    assert r["reason"] is None and r["can_cancel"] is False


def test_cancel_is_one_fixed_op():
    action = FollowupCancel(action="followup_cancel", key="DEMO-0001", id=FU)
    assert host_call(action) == ("follow_up.cancel", {"id_or_ref": FU})
    ok = expected(action)
    event = {"type": "followup.cancelled", "followup": FU, "reason": "person"}
    assert ok([event])
    assert not ok([{**event, "followup": FU[:-1] + "1"}])
    assert not ok([{**event, "reason": "dismissed"}])
    assert not ok([event, event])


def test_a_follow_up_grant_is_listed_with_its_parent():
    from orch_dashboard.adapters import settings

    parent, child = "gr_" + "0" * 25 + "1", "fgr_" + "0" * 25 + "2"
    base = {
        "person": "p_1",
        "scope": "t",
        "verbs": "all",
        "issued_at": "2026-10-27T09:00:00Z",
        "expires_at": "2026-10-27T17:00:00Z",
        "sessions": 0,
    }
    out = settings.grants({"grants": [{**base, "id": parent}, {**base, "id": child, "parent": parent}]}, ext=False)
    assert [g.parent for g in out] == [None, parent]


@pytest.mark.parametrize(
    "cls,action,extra,args",
    [
        (FollowupCancel, "followup_cancel", {}, {"id_or_ref": FU}),
        (
            FollowupReschedule,
            "followup_reschedule",
            {"when": "2026-10-28T09:00:00Z"},
            {"id": FU, "when": "2026-10-28T09:00:00Z"},
        ),
        (FollowupUndo, "followup_undo", {}, {"id": FU}),
    ],
)
def test_cancel_names_the_ticket_the_follow_up_is_on(gateway, stub, monkeypatch, cls, action, extra, args):
    import pytest

    from orch_dashboard import signing as handoff
    from orch_dashboard.errors import HostError

    seen = []

    def call(ws, op, args=None, **kw):
        seen.append((op, args))
        return {"rows": [{"id": FU}]} if op == "follow_up.list" else {}

    link = gateway.enabled(stub.ws)
    monkeypatch.setattr(gateway, "link", lambda ws: link)
    monkeypatch.setattr(gateway, "call", call)
    ok = cls(action=action, key="DEMO-0001", id=FU, **extra)
    assert handoff.plan(gateway, stub.ws, ok).args == args
    other = cls(action=action, key="DEMO-0001", id=FU[:-1] + "1", **extra)
    with pytest.raises(HostError):
        handoff.plan(gateway, stub.ws, other)
    assert ("follow_up.list", {"ref": "DEMO-0001", "all": True}) in seen


def test_only_a_revoke_may_name_a_follow_up_grant():
    from orch_dashboard import signing as handoff

    fgr = "fgr_" + "0" * 26
    assert handoff.GrantRevoke(action="grant_revoke", grant=fgr).grant == fgr
    for cls, extra in ((handoff.RunnerGrant, {"hours": 1, "scope": "workable", "extend": fgr}),):
        with pytest.raises(Exception):  # noqa: B017, PT011
            cls(action="runner_grant", **extra)


@pytest.mark.parametrize("field", ["due", "deadline"])
def test_reschedule_is_one_fixed_op_and_exact_event(field):
    from orch_dashboard.signing import FollowupReschedule

    when = "2026-10-28T09:00:00.123Z"
    action = FollowupReschedule(action="followup_reschedule", key="DEMO-0001", id=FU, when=when)
    assert host_call(action) == ("follow_up.reschedule", {"id": FU, "when": when})
    ok = expected(action)
    event = {"type": "followup.rescheduled", "followup": FU, field: "2026-10-28T09:00:00Z"}
    assert ok([event])
    for wrong in [
        {**event, "followup": FU[:-1] + "1"},
        {**event, field: "2026-10-28T09:00:01Z"},
        {**event, "due": when, "deadline": when},
        {**event, field: None},
        {"type": "followup.rescheduled", "followup": FU},
    ]:
        assert not ok([wrong])
    assert not ok([event, event])
    assert not ok([])


@pytest.mark.parametrize("when", ["2026-10-28", "2026-10-28T09:00:00+00:00", "tomorrow"])
def test_reschedule_requires_utc_instant(when):
    from pydantic import ValidationError

    from orch_dashboard.signing import FollowupReschedule

    with pytest.raises(ValidationError):
        FollowupReschedule(action="followup_reschedule", key="DEMO-0001", id=FU, when=when)


def test_undo_is_one_fixed_op_and_exact_event():
    from orch_dashboard.signing import FollowupUndo

    action = FollowupUndo(action="followup_undo", key="DEMO-0001", id=FU)
    assert host_call(action) == ("follow_up.undo", {"id": FU})
    ok = expected(action)
    event = {"type": "followup.restored", "followup": FU}
    assert ok([event])
    assert not ok([{**event, "followup": FU[:-1] + "1"}])
    assert not ok([{**event, "type": "followup.cancelled"}])
    assert not ok([event, event])


@pytest.mark.parametrize("reschedule,undo", [(True, False), (False, True), (True, True), (False, False)])
@pytest.mark.parametrize("role", ["member", "viewer"])
def test_action_support_flags(client, token, stub, gateway, fu_host, monkeypatch, reschedule, undo, role):
    link = gateway.enabled(stub.ws)
    for op, supported in [("follow_up.reschedule", reschedule), ("follow_up.undo", undo)]:
        if supported:
            link.welcome["ops"][op] = 1
        else:
            link.welcome["ops"].pop(op, None)
    original = link.call

    def call(op, args=None, **kwargs):
        if op == "member.list":
            data = original(op, args, **kwargs)
            return {
                **data,
                "members": [{**m, "role": role} if m["person"] == stub.person else m for m in data["members"]],
            }
        return original(op, args, **kwargs)

    monkeypatch.setattr(link, "call", call)
    got = client.get(f"/api/w/{stub.ws}/followups", headers=headers(token))
    assert got.json()["can"] == {
        "cancel": role != "viewer",
        "reschedule": reschedule and role != "viewer",
        "undo": undo and role != "viewer",
    }


@pytest.mark.parametrize("state", ["pending", "needs_person", "cancelled", "fired"])
@pytest.mark.parametrize("mine,role", [(True, "member"), (False, "member"), (False, "owner"), (False, "maintainer")])
def test_row_reschedule_permission(state, mine, role):
    result = followups.row({"id": FU, "state": state, "person": "me" if mine else "other"}, {}, "me", role)
    assert result["can_reschedule"] == (state in ("pending", "needs_person") and (mine or role in followups.MANAGERS))
