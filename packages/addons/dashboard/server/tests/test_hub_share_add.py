"""Share more from the dashboard (`share.rule_add`, a confirming `share.sync`): click-only, full LOCAL session, owner as
the session's person, `expect` must be the plan of a preview this session was shown, and the review is the host's."""

import logging

import pytest
from remote_kit import HTTPS, app, bearer, front, passkey_store, remote  # noqa: F401
from test_hub_share_reduce import MEMBER, OWNER, PLAN, link_of, writes
from test_market_routes import hdr

from orch_dashboard.errors import HostError
from orch_dashboard.policy import LOOPBACK

SHOWN = "B" * 43
REVIEW = {
    "hub_id": "ab" * 8,
    "readers": {"people": ["Ben Test"], "teams": ["team-a"], "note": "from the hub, not signed"},
    "widened": [],
    "new": {"count": 1, "more": 0, "items": [{"logical": "t", "kind": "ticket", "line": "ticket K-2: Second"}]},
    "leaves": [{"kind": "ticket", "text": "the ticket's title, status, labels and deadline"}],
    "later_edits": "Later edits are shared too, until you stop sharing.",
    "leaves_short": "the title, status and deadline",
    "later_edits_short": "Later edits are shared too.",
}
PREVIEW = {"count": 2, "new": 1, "revoked": 0, "more": 0, "items": [], "rules": [], "plan": SHOWN, "review": REVIEW}
RULE = {"kind": "tag", "value": "public"}


@pytest.fixture
def host(gateway, monkeypatch):
    fake = link_of(OWNER, "owner")
    fake.supports = lambda op, cap: True
    fake.answers["share.preview"] = PREVIEW
    fake.answers["share.rule_add"] = {"note": "changed", "synced": True, "put": 1}
    fake.answers["share.sync"] = {"synced": False, "pending": "hub_unreachable", "put": 0}
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    monkeypatch.setattr(gateway, "link", lambda ws: fake)
    return fake


def show(client, stub, token, body=None):
    r = client.post(f"/api/w/{stub.ws}/hub/share/preview", json=body or {"add": RULE}, headers=hdr(token))
    assert r.status_code == 200, r.text
    return r.json()


def add(client, stub, token, plan=SHOWN, path="rule-add", headers=None):
    body = {"rule": RULE, "expect": plan} if path == "rule-add" else {"expect": plan}
    return client.post(f"/api/w/{stub.ws}/hub/share/{path}", json=body, headers=headers or hdr(token))


def test_the_review_is_the_hosts_closed_shape(client, token, stub, host):
    review = show(client, stub, token)["review"]
    assert review["hub_id"] == "ab" * 8 and review["readers"]["people"] == ["Ben Test"]
    assert review["new_count"] == 1 and review["new_items"] == ["ticket K-2: Second"]
    assert review["leaves"] and review["later_edits"].startswith("Later edits") and review["leaves_short"] == "the title, status and deadline"


def test_a_shown_plan_is_sent_once_as_the_sessions_person(client, token, stub, host, caplog):
    show(client, stub, token)
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.security"):
        r = add(client, stub, token)
    assert r.status_code == 200 and r.json() == {"synced": True, "pending": None, "put": 1}
    ((op, args, write, idem),) = writes(host)
    assert (op, write) == ("share.rule_add", True) and idem.startswith("a_")
    assert args == {"rule": {"kind": "tag", "tag": "public"}, "expect": SHOWN}
    assert OWNER in caplog.text
    assert add(client, stub, token).status_code == 400  # a plan confirms once; the next press needs a new review
    assert len(writes(host)) == 1


def test_a_plan_that_was_never_shown_here_reaches_nothing(client, token, stub, host):
    for path in ("rule-add", "sync"):
        r = add(client, stub, token, plan=PLAN, path=path)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid.input", path
    assert writes(host) == []


def test_the_plan_one_session_saw_is_not_valid_for_another(client, token, stub, host, auth):
    show(client, stub, token)
    other = auth.open_switch(LOOPBACK, stub.ws)["token"]
    assert add(client, stub, other).status_code in (400, 403)
    assert writes(host) == []


def test_sync_confirms_a_shown_plan_and_tells_when_the_hub_is_away(client, token, stub, host):
    show(client, stub, token, {})
    r = add(client, stub, token, path="sync")
    assert r.status_code == 200 and r.json() == {"synced": False, "pending": "hub_unreachable", "put": 0}
    assert writes(host)[0][:2] == ("share.sync", {"expect": SHOWN})


def test_a_switch_session_is_read_only(client, auth, token, stub, host):
    show(client, stub, token)
    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    for path in ("rule-add", "sync"):
        r = add(client, stub, switch, path=path, headers=hdr(switch))
        assert r.status_code == 403 and r.json()["error"]["code"] == "session.readonly", path
    assert writes(host) == []


def test_a_member_is_refused_before_the_write(client, token, stub, gateway, monkeypatch):
    member = link_of(MEMBER, "member")
    member.supports = lambda op, cap: True
    member.answers["share.preview"] = PREVIEW
    monkeypatch.setattr(gateway, "enabled", lambda ws: member)
    monkeypatch.setattr(gateway, "link", lambda ws: member)
    show(client, stub, token)
    r = add(client, stub, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert writes(member) == []


def test_the_host_saying_no_is_passed_on_not_logged_as_sent(client, token, stub, host, caplog):
    host.answers["share.rule_add"] = HostError(
        {"code": "op.not_allowed", "message": "a person does this at their own terminal", "retryable": False}, 3
    )
    show(client, stub, token)
    r = add(client, stub, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"


def test_five_a_minute_is_the_hosts_limit_and_is_passed_on(client, token, stub, host):
    host.answers["share.rule_add"] = HostError({"code": "host.busy", "message": "wait a minute", "retryable": True}, 4)
    show(client, stub, token)
    assert add(client, stub, token).json()["error"]["code"] == "host.busy"


def test_bad_bodies_reach_nothing(client, token, stub, host):
    url = f"/api/w/{stub.ws}/hub/share/rule-add"
    for body in ({"rule": RULE}, {"rule": RULE, "expect": "short"}, {"rule": RULE, "expect": SHOWN, "x": 1},
                 {"rule": {"kind": "tag"}, "expect": SHOWN}):  # fmt: skip
        assert client.post(url, json=body, headers=hdr(token)).status_code == 400
    assert writes(host) == []
