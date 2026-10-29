"""Stop sharing from the dashboard: `share.revoke` and `share.rule_remove` follow the cosmetic precedent
(`test_workspace_icon.py`): one click, no signature, owner only, from a full session, as the session's person. A switch
session is told `session.readonly`; a GET never reaches a host write."""

import logging

import pytest
from remote_kit import HTTPS, app, bearer, front, passkey_store, remote  # noqa: F401
from test_market_routes import Host, hdr

from orch_dashboard.hub import preview_view
from orch_dashboard.policy import LOOPBACK

OWNER = "p_" + "1" * 32
MEMBER = "p_" + "2" * 32
PLAN = "A" * 43
DONE = {"note": "withdrawn; what the hub already read cannot be taken back", "synced": True, "put": 0, "revoke": 1}


def link_of(person, role):
    fake = Host()
    fake.welcome = {"peer": {"person": person}}
    fake.answers["member.list"] = {"members": [{"person": person, "role": role}]}
    fake.answers["share.revoke"] = DONE
    fake.answers["share.rule_remove"] = {"note": "changed", "synced": False, "pending": "hub_unreachable"}
    return fake


@pytest.fixture
def host(gateway, monkeypatch):
    fake = link_of(OWNER, "owner")
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    monkeypatch.setattr(gateway, "link", lambda ws: fake)  # the session's person (`person_of`) is the link's here
    return fake


def writes(host):
    return [c for c in host.calls if c[2]]


def revoke(client, stub, token, body=None, headers=None):
    return client.post(
        f"/api/w/{stub.ws}/hub/share/revoke", json=body or {"ref": "PRD-0014"}, headers=headers or hdr(token)
    )


def test_a_full_session_of_the_owner_revokes_as_its_person(client, token, stub, host, caplog):
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.security"):
        r = revoke(client, stub, token)
    assert r.status_code == 200 and r.json() == {"synced": True, "pending": None, "revoked": 1}
    assert [c[0] for c in host.calls] == ["member.list", "share.revoke"]
    ((op, args, write, idem),) = writes(host)
    assert (op, args, write) == ("share.revoke", {"ref": "PRD-0014"}, True) and idem.startswith("a_")
    (line,) = [r for r in caplog.records if r.name == "orch_dashboard.security"]
    assert "share.revoke" in line.getMessage() and OWNER in line.getMessage()


def test_a_second_revoke_is_the_same_answer_with_a_new_idem(client, token, stub, host):
    assert revoke(client, stub, token).status_code == 200
    assert revoke(client, stub, token).status_code == 200
    first, second = writes(host)
    assert first[:3] == second[:3] and first[3] != second[3]  # the host op is idempotent itself


def test_a_rule_removal_sends_the_plan_that_was_shown(client, token, stub, host):
    body = {"rule": {"kind": "ticket", "value": "PRD-0014"}, "expect": PLAN}
    r = client.post(f"/api/w/{stub.ws}/hub/share/rule-remove", json=body, headers=hdr(token))
    assert r.status_code == 200 and r.json() == {"synced": False, "pending": "hub_unreachable", "revoked": 0}
    ((op, args, write, _),) = writes(host)
    assert (op, write) == ("share.rule_remove", True)
    assert args == {"rule": {"kind": "ticket", "ticket": "PRD-0014"}, "expect": PLAN}


def test_a_rule_removal_carries_the_readers_and_description_the_row_listed(client, token, stub, host):
    """Run 9 (J03): core matches a removal exactly, so the rule goes to the host with its readers and flag."""
    aud = {"users": ["a" * 32], "groups": ["team-a"]}
    body = {"rule": {"kind": "ticket", "value": "PW-0001", "audience": aud, "description": True}, "expect": PLAN}
    r = client.post(f"/api/w/{stub.ws}/hub/share/rule-remove", json=body, headers=hdr(token))
    assert r.status_code == 200
    ((op, args, _w, _),) = writes(host)
    assert op == "share.rule_remove"
    assert args == {
        "rule": {"kind": "ticket", "ticket": "PW-0001", "audience": aud, "description": True},
        "expect": PLAN,
    }


def test_removing_a_rule_the_host_does_not_have_is_refused_and_not_logged(client, token, stub, host, caplog):
    from orch_dashboard.errors import HostError

    message = "share: that rule is not in the list; nothing changed"
    host.answers["share.rule_remove"] = HostError({"code": "not_found", "message": message, "retryable": False}, 2)
    body = {"rule": {"kind": "tag", "value": "absent"}, "expect": PLAN}
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.security"):
        r = client.post(f"/api/w/{stub.ws}/hub/share/rule-remove", json=body, headers=hdr(token))
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert [c[0] for c in writes(host)] == ["share.rule_remove"]
    assert not [r for r in caplog.records if r.name == "orch_dashboard.security"]  # nothing was taken away


def test_bad_bodies_reach_nothing(client, token, stub, host):
    url = f"/api/w/{stub.ws}/hub/share/rule-remove"
    for body in (
        {"rule": {"kind": "tag", "value": "x"}},  # no plan: the owner must have been shown the preview
        {"rule": {"kind": "tag", "value": "x"}, "expect": "short"},
        {"rule": {"kind": "tag", "value": "x"}, "expect": PLAN, "extra": 1},
    ):
        assert client.post(url, json=body, headers=hdr(token)).status_code == 400
    assert revoke(client, stub, token, {"ref": "a b"}).status_code == 400
    assert revoke(client, stub, token, {"ref": "-x"}).status_code == 400
    assert host.calls == []


def test_a_member_is_refused_before_the_write(client, token, stub, gateway, monkeypatch):
    member = link_of(MEMBER, "member")
    monkeypatch.setattr(gateway, "enabled", lambda ws: member)
    monkeypatch.setattr(gateway, "link", lambda ws: member)
    r = revoke(client, stub, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert writes(member) == []


def test_the_sessions_person_must_be_the_owner_not_the_links_peer(client, token, stub, host, monkeypatch):
    """The dashboard link's peer is the owner, but this session's person is a member: refused before any write."""
    host.answers["member.list"] = {
        "members": [{"person": OWNER, "role": "owner"}, {"person": MEMBER, "role": "member"}]
    }
    monkeypatch.setattr(client.app.state, "person_of", lambda ws: MEMBER)
    remove = {"rule": {"kind": "needs_you"}, "expect": PLAN}
    for path, body in (("revoke", {"ref": "PRD-0014"}), ("rule-remove", remove)):
        r = client.post(f"/api/w/{stub.ws}/hub/share/{path}", json=body, headers=hdr(token))
        assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied", path
    assert writes(host) == []


def test_a_switch_session_is_read_only(client, auth, stub, host):
    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    remove = {"rule": {"kind": "needs_you"}, "expect": PLAN}
    for path, body in (("revoke", {"ref": "PRD-0014"}), ("rule-remove", remove)):
        r = client.post(f"/api/w/{stub.ws}/hub/share/{path}", json=body, headers=hdr(switch))
        assert r.status_code == 403 and r.json()["error"]["code"] == "session.readonly", path
    assert host.calls == []


def test_a_remote_session_without_its_passkey_is_refused(front, auth, remote, stub, host):  # noqa: F811
    r = revoke(front, stub, remote, headers=bearer(remote, HTTPS))
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"
    assert host.calls == []


def test_a_remote_passkey_session_of_the_owner_is_forwarded(front, auth, stub, host):  # noqa: F811
    passkey = auth.open_remote(credential_id="cred-1")["token"]
    assert revoke(front, stub, passkey, headers=bearer(passkey, HTTPS)).status_code == 200
    assert [c[0] for c in writes(host)] == ["share.revoke"]


def test_a_get_never_writes(client, token, stub, host):
    for path in ("revoke", "rule-remove"):
        assert client.get(f"/api/w/{stub.ws}/hub/share/{path}", headers=hdr(token, write=False)).status_code in (
            404,
            405,
        )
    assert writes(host) == []


def test_the_preview_carries_its_plan_only_in_the_closed_shape():
    assert preview_view({"plan": PLAN})["plan"] == PLAN
    assert preview_view({"plan": "x" * 44})["plan"] is None
    assert preview_view({})["plan"] is None
