"""D6, the server side: the cosmetic route is a thin proxy to `workspace.cosmetic.set` (no signature) that acts as the
browser session's person from a full session only, and only a plain pair of the closed lists reaches the page."""

import pytest
from conftest import PORT
from remote_kit import HTTPS, app, bearer, front, passkey_store, remote  # noqa: F401
from test_market_routes import Host, hdr

from orch_dashboard.adapters.shell import workspace_icon
from orch_dashboard.cosmetic_routes import full_session
from orch_dashboard.policy import LOOPBACK, REMOTE

PAIR = {"icon": "rocket", "colour": "pink"}
CUR = {"name": "Studio", "icon": None}
OWNER = "p_" + "1" * 32
MEMBER = "p_" + "2" * 32


def link_of(person, role):
    """A fake workspace link whose peer is ``person`` (what the host sees as `call.viewer`) with ``role``."""
    fake = Host()
    fake.welcome = {"peer": {"person": person}}
    fake.answers["member.list"] = {
        "members": [{"person": person, "role": role}, {"person": "p_" + "9" * 32, "role": "owner"}]
    }
    return fake


@pytest.fixture
def host(gateway, monkeypatch):
    fake = link_of(OWNER, "owner")
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    return fake


def writes(host):
    return [c for c in host.calls if c[2]]


def put(client, stub, token, origin=None, body=None):
    headers = hdr(token) if origin is None else bearer(token, origin)
    return client.put(
        f"/api/w/{stub.ws}/workspace/cosmetic", json=body or {"name": "Lab", "if_current": CUR}, headers=headers
    )


def test_the_route_sends_the_change_and_the_compare_value(client, token, stub, host):
    r = client.put(
        f"/api/w/{stub.ws}/workspace/cosmetic",
        json={"name": "Lab", "icon": PAIR, "if_current": CUR},
        headers=hdr(token),
    )
    assert r.status_code == 200
    ((op, args, write, idem),) = writes(host)
    assert (op, write) == ("workspace.cosmetic.set", True) and idem.startswith("a_")
    assert args == {"name": "Lab", "icon": PAIR, "if_current": CUR}


def test_null_icon_clears_and_an_omitted_icon_is_left_alone(client, token, stub, host):
    client.put(f"/api/w/{stub.ws}/workspace/cosmetic", json={"icon": None, "if_current": CUR}, headers=hdr(token))
    client.put(f"/api/w/{stub.ws}/workspace/cosmetic", json={"name": "X", "if_current": CUR}, headers=hdr(token))
    assert writes(host)[0][1] == {"icon": None, "if_current": CUR}
    assert writes(host)[1][1] == {"name": "X", "if_current": CUR}


def test_extra_fields_and_a_missing_compare_value_are_refused(client, token, stub, host):
    url = f"/api/w/{stub.ws}/workspace/cosmetic"
    assert client.put(url, json={"name": "X"}, headers=hdr(token)).status_code == 400
    assert client.put(url, json={"name": "X", "if_current": CUR, "x": 1}, headers=hdr(token)).status_code == 400
    assert host.calls == []


# ---- who: the session's person, from a full session --------------------------------------------------------------
def test_an_owner_session_is_forwarded_as_its_own_person(client, token, stub, host):
    """The write goes over the link whose peer is the session's person (the host's `call.viewer`), after the
    dashboard read that this very person is the owner."""
    assert put(client, stub, token).status_code == 200
    assert [c[0] for c in host.calls] == ["member.list", "workspace.cosmetic.set"]
    assert host.welcome["peer"]["person"] == OWNER


def test_a_member_session_is_refused_before_the_write(client, token, stub, gateway, monkeypatch):
    member = link_of(MEMBER, "member")
    monkeypatch.setattr(gateway, "enabled", lambda ws: member)
    r = put(client, stub, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert writes(member) == []


def test_a_switch_session_is_refused(client, auth, stub, host):
    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    assert not full_session(auth, switch)  # the handler's own guard, beside the security layer's default
    r = put(client, stub, switch)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"
    assert host.calls == []


def test_a_remote_session_without_its_passkey_is_refused(front, auth, remote, stub, host):  # noqa: F811
    assert not full_session(auth, remote)
    r = put(front, stub, remote, HTTPS)
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"
    assert host.calls == []


def test_a_remote_switch_session_is_refused(front, auth, stub, host):  # noqa: F811
    switch = auth.open_switch(REMOTE, stub.ws)["token"]
    assert not full_session(auth, switch)
    r = put(front, stub, switch, HTTPS)
    assert r.status_code == 403 and r.json()["error"]["code"] == "step_up_required"
    assert host.calls == []


def test_a_remote_passkey_session_of_the_owner_is_forwarded(front, auth, stub, host):  # noqa: F811
    passkey = auth.open_remote(credential_id="cred-1")["token"]
    assert full_session(auth, passkey)
    assert put(front, stub, passkey, HTTPS).status_code == 200
    assert [c[0] for c in writes(host)] == ["workspace.cosmetic.set"]


def test_a_local_session_is_full(auth):
    local = auth.exchange(auth.launch(PORT)["code"], "local")[0]["token"]
    assert full_session(auth, local)
    assert not full_session(auth, "made-up")


def test_only_a_plain_pair_of_the_lists_reaches_the_page():
    assert workspace_icon({"icon": PAIR}) == PAIR
    assert workspace_icon({"icon": None}) is None
    assert workspace_icon({}) is None
    assert workspace_icon({"icon": {"icon": "<img src=x>", "colour": "pink"}}) is None
    assert workspace_icon({"icon": {**PAIR, "extra": 1}}) is None
