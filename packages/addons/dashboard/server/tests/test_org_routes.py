"""Company skills and tools (M3 H-5): the owner's plain clicks over the host's org.* ops. The closed views keep only
checked fields; text with a hidden character is withheld; turning ON is a full LOCAL session; a switch session and a
member are refused before any write."""

import logging

import pytest
from remote_kit import HTTPS, app, bearer, front, passkey_store, remote  # noqa: F401
from test_hub_share_reduce import MEMBER, OWNER, link_of
from test_market_routes import Host, hdr

from orch_dashboard.org_routes import item_view, offers_view
from orch_dashboard.policy import LOOPBACK

D1 = "sha256:" + "a" * 64
D2 = "sha256:" + "b" * 64
OFFERS = {
    "hub_id": "9f3c1ab277de40c8",
    "skills": [
        {"name": "style-guide", "digest": D1, "status": "off", "extra": "dropped"},
        {"name": "Bad Name", "digest": D1, "status": "on"},
    ],
    "mcp": [{"name": "crm", "digest": D2, "status": "needs_isolation", "auth": "hub_secret"}],
}


class OrgHost(Host):
    def supports(self, op, cap="ops/1"):
        return True


@pytest.fixture
def host(gateway, monkeypatch):
    fake = OrgHost()
    fake.welcome = {"peer": {"person": OWNER}}
    fake.answers = link_of(OWNER, "owner").answers
    fake.answers["org.offers"] = OFFERS
    fake.answers["org.accept"] = {"kind": "skill", "name": "style-guide", "status": "on"}
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    monkeypatch.setattr(gateway, "link", lambda ws: fake)
    return fake


def writes(host):
    return [c for c in host.calls if c[2]]


def test_offers_keep_only_checked_fields(client, token, stub, host):
    r = client.get(f"/api/w/{stub.ws}/org", headers=hdr(token, write=False))
    assert r.status_code == 200
    assert r.json() == {
        "available": True,
        "hub_id": "9f3c1ab277de40c8",
        "skills": [{"name": "style-guide", "digest": D1, "status": "off"}],
        "mcp": [{"name": "crm", "digest": D2, "status": "needs_isolation", "auth": "hub_secret"}],
    }


def test_a_skill_is_shown_as_text_and_hidden_characters_withhold_it():
    ok = item_view(
        {
            "kind": "skill",
            "status": "off",
            "item": {"name": "style-guide", "text": "a\n\t**b** <i>", "digest": D1},
            "notes": ["n"],
            "untrusted": True,
        }
    )
    assert ok["item"]["text"] == "a\n\t**b** <i>" and ok["item"]["text_hidden"] is False
    bad = item_view({"kind": "skill", "status": "off", "item": {"name": "style-guide", "text": "a‮b", "digest": D1}})
    assert bad["item"]["text"] is None and bad["item"]["text_hidden"] is True


def test_a_server_view_is_closed_and_tools_are_shown_in_full():
    long_desc = "d" * 3000 + "\nsecond line"
    got = item_view(
        {
            "kind": "mcp",
            "status": "off",
            "item": {
                "name": "crm",
                "url": "https://crm.example.com/mcp",
                "auth": "hub_secret",
                "secret": "crm-key",
                "description": "CRM",
                "digest": D2,
                "x": 1,
            },
            "tools": [
                {
                    "name": "find",
                    "title": "Find",
                    "description": long_desc,
                    "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    "pin": "p1",
                    "status": "on",
                },
                {"name": "x", "status": "weird"},
                {"name": "huge", "status": "too_long", "pin": "leak", "description": "leak"},
                {"name": "esc", "description": "a\u202eb", "pin": "p2", "status": "off"},
            ],
            "needs_isolation": "needs a separate agent user",
        }
    )
    tools = got["item"]["tools"]
    assert [t["name"] for t in tools] == ["find", "huge", "esc"]
    assert tools[0]["description"] == long_desc and tools[0]["title"] == "Find" and tools[0]["pin"] == "p1"
    assert '"type": "object"' in tools[0]["input_schema"]
    assert tools[1] == {
        "name": "huge",
        "title": None,
        "description": None,
        "input_schema": None,
        "pin": "",
        "status": "too_long",
        "reads": False,
    }
    assert tools[2]["description"] is None  # a hidden character withholds that text
    assert got["item"]["url"] == "https://crm.example.com/mcp" and got["item"]["secret"] == "crm-key"
    assert got["needs_isolation"] == "needs a separate agent user"
    assert offers_view(None)["skills"] == []


def test_accept_sends_the_shown_digest_as_the_owner(client, token, stub, host, caplog):
    body = {"kind": "skill", "name": "style-guide", "digest": D1}
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.security"):
        r = client.post(f"/api/w/{stub.ws}/org/accept", json=body, headers=hdr(token))
    assert r.status_code == 200 and r.json() == {"kind": "skill", "name": "style-guide", "status": "on"}
    ((op, args, write, idem),) = writes(host)
    assert (op, args, write) == ("org.accept", body, True) and idem.startswith("a_")
    assert [r for r in caplog.records if r.name == "orch_dashboard.security" and "org.accept" in r.getMessage()]


def test_decline_and_tool_off_and_sync(client, token, stub, host):
    host.answers["org.sync"] = {"result": {"skill": "ok", "mcp": "unknown_op"}}
    assert (
        client.post(
            f"/api/w/{stub.ws}/org/decline", json={"kind": "mcp", "name": "crm"}, headers=hdr(token)
        ).status_code
        == 200
    )
    off = {"server": "crm", "tool": "find", "on": False}
    assert client.post(f"/api/w/{stub.ws}/org/tool", json=off, headers=hdr(token)).json() == off
    r = client.post(f"/api/w/{stub.ws}/org/sync", json={}, headers=hdr(token))
    assert r.json() == {"result": {"skill": "ok", "mcp": "unknown_op"}}
    assert [c[:2] for c in writes(host)] == [
        ("org.decline", {"kind": "mcp", "name": "crm"}),
        ("org.tool", off),
        ("org.sync", {}),
    ]


def test_tool_on_needs_a_pin_and_sends_it(client, token, stub, host):
    url = f"/api/w/{stub.ws}/org/tool"
    assert client.post(url, json={"server": "crm", "tool": "find", "on": True}, headers=hdr(token)).status_code == 400
    assert writes(host) == []
    on = {"server": "crm", "tool": "find", "on": True, "pin": "p1"}
    assert client.post(url, json=on, headers=hdr(token)).status_code == 200
    assert writes(host)[0][:2] == ("org.tool", on)


def test_the_bulk_click_sends_the_listed_tools_and_pins_as_the_owner(client, token, stub, host):
    url = f"/api/w/{stub.ws}/org/tools"
    body = {"server": "crm", "tools": [{"tool": "find", "pin": "p1"}, {"tool": "list", "pin": "p2"}]}
    assert client.post(url, json=body, headers=hdr(token)).status_code == 200
    assert writes(host)[0][:2] == ("org.tool", {"server": "crm", "on": True, "tools": body["tools"]})
    for bad in ({"server": "crm", "tools": []}, {"server": "crm", "tools": [{"tool": "find"}]},
                {"server": "crm", "tools": [{"tool": "find", "pin": "p"}], "on": False}):  # fmt: skip
        assert client.post(url, json=bad, headers=hdr(token)).status_code in (400, 422)
    assert len(writes(host)) == 1


def test_a_reads_label_is_only_true_when_the_host_says_true():
    from orch_dashboard.org_routes import _tools

    base = {"name": "a", "description": "d", "inputSchema": {"type": "object"}, "pin": "p", "status": "off"}
    got = [_tools([{**base, "reads": v}])[0][0]["reads"] for v in (True, False, "true", 1, None)]
    assert got == [True, False, False, False, False]


def test_bad_bodies_reach_nothing(client, token, stub, host):
    for path, body in (
        ("accept", {"kind": "skill", "name": "style-guide", "digest": "sha256:short"}),
        ("accept", {"kind": "other", "name": "style-guide", "digest": D1}),
        ("decline", {"kind": "skill", "name": "Bad Name"}),
        ("decline", {"kind": "skill", "name": "ok-name", "extra": 1}),
    ):
        assert client.post(f"/api/w/{stub.ws}/org/{path}", json=body, headers=hdr(token)).status_code == 400, path
    assert host.calls == []


def test_a_member_is_refused_before_the_write(client, token, stub, gateway, monkeypatch):
    member = OrgHost()
    member.welcome = {"peer": {"person": MEMBER}}
    member.answers = link_of(MEMBER, "member").answers
    monkeypatch.setattr(gateway, "enabled", lambda ws: member)
    monkeypatch.setattr(gateway, "link", lambda ws: member)
    r = client.post(f"/api/w/{stub.ws}/org/decline", json={"kind": "skill", "name": "style-guide"}, headers=hdr(token))
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert writes(member) == []


def test_a_switch_session_is_read_only(client, auth, stub, host):
    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    for path, body in (
        ("accept", {"kind": "skill", "name": "style-guide", "digest": D1}),
        ("decline", {"kind": "skill", "name": "style-guide"}),
        ("tool", {"server": "crm", "tool": "find", "on": False}),
    ):
        r = client.post(f"/api/w/{stub.ws}/org/{path}", json=body, headers=hdr(switch))
        assert r.status_code == 403 and r.json()["error"]["code"] == "session.readonly", path
    r = client.post(f"/api/w/{stub.ws}/org/tools", json={"server": "crm", "tools": [{"tool": "find", "pin": "p1"}]},
                    headers=hdr(switch))  # fmt: skip
    assert r.status_code == 403 and r.json()["error"]["code"] in ("session.readonly", "step_up_required")  # turning On
    assert host.calls == []


def test_an_older_host_degrades(client, token, stub, gateway, monkeypatch):
    class Old(OrgHost):
        def supports(self, op, cap="ops/1"):
            return False

    old = Old()
    monkeypatch.setattr(gateway, "enabled", lambda ws: old)
    r = client.get(f"/api/w/{stub.ws}/org", headers=hdr(token, write=False))
    assert r.status_code == 200 and r.json()["available"] is False


def test_a_tool_that_cannot_be_shown_whole_cannot_be_switched_on():
    """approved view == pinned view: a withheld description, title or schema makes the tool unswitchable."""
    from orch_dashboard.org_routes import _tools

    pin = "sha256:" + "a" * 64
    good = {"name": "t", "description": "ok", "inputSchema": {"type": "object"}, "pin": pin, "status": "off"}
    assert _tools([good])[0][0]["pin"] == pin
    for bad in ({"description": "a\u200bb"}, {"title": "x\u202e"}, {"inputSchema": None}):
        row = _tools([{**good, **bad}])[0][0]
        assert row["status"] == "too_long" and row["pin"] == ""


def test_a_field_that_cannot_be_shown_marks_the_server_and_dropped_tools_are_counted():
    item = {
        "name": "crm",
        "url": "https://crm.example.com/mcp",
        "auth": "none",
        "secret": None,
        "description": "CRM",
        "digest": D2,
    }
    tools = [
        {"name": "ok", "description": "d", "inputSchema": {}, "pin": "p", "status": "off"},
        {"name": "nopin", "status": "off"},
        {"status": "on"},
    ]

    def view(**over):
        return item_view({"kind": "mcp", "status": "off", "item": item | over, "tools": tools})["item"]

    assert view()["fields_hidden"] is False and view()["hidden_tools"] == 2
    assert view(url="https://crm.exam\u202eple.com/mcp")["fields_hidden"] is True
    assert view(description="a\u202eb")["fields_hidden"] is True
    assert view(auth="hub_secret", secret=None)["fields_hidden"] is True
    assert view(auth="hub_secret", secret="crm-key")["fields_hidden"] is False
