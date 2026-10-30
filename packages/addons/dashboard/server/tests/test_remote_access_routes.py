"""R2 T4 (#1590): the loopback page of remote access. Reads and the owner's plain clicks (off, code, remove a device)
go to the host as the session's person; turning ON and confirming a device are clicks with the backup key
(`signing.RemoteSet`, `RemoteDeviceConfirm`). Nothing here is reachable from the remote listener."""

# ruff: noqa: F811

import logging

import pytest
from remote_kit import HTTPS, app, bearer, front, passkey_store, remote  # noqa: F401
from test_market_routes import Host, hdr

from orch_dashboard import routegate, signing
from orch_dashboard.policy import LOOPBACK
from orch_dashboard.remote_routes import view

OWNER = "p_" + "1" * 32
MEMBER = "p_" + "2" * 32
DEV = "rd_" + "a" * 32
PENDING = "rp_" + "b" * 32
CODE = "orch-rc1.w_abc.AAAA.BBBB"
STATUS = {
    "on": True, "level": "work", "until_ms": 5000, "premises": "no", "off_reason": None, "isolated": False,
    "hub_mode": "selective", "agents_risk": True, "codes": 1, "wrk_fp": "00" * 16, "device_version": 3,
    "devices": [{
        "device_id": DEV, "name": "Anna's laptop", "person": OWNER, "presence_kind": "platform", "level_cap": "view",
        "added_ms": 1, "expires_ms": 9000, "words": "one two three four five six",
    }],
}  # fmt: skip
ROUTES = (
    ("GET", "/remote"),
    ("POST", "/remote/off"),
    ("POST", "/remote/code"),
    ("POST", f"/remote/devices/{DEV}/remove"),
)


def link_of(person, role):
    fake = Host()
    fake.welcome = {"peer": {"person": person}}
    fake.answers["member.list"] = {"members": [{"person": person, "role": role}]}
    fake.answers["remote.status"] = STATUS
    fake.answers["remote.pending"] = {
        "pending": [{"pending": PENDING, "name": "Anna's phone", "words": "a b c d e f", "presence_kind": "fido2",
                     "expires_ms": 77}]
    }  # fmt: skip
    fake.answers["remote.sessions"] = {"sessions": [{"stream_id": "s" * 32, "device_id": DEV}]}
    fake.answers["remote.off"] = {"changed": True}
    fake.answers["remote.code"] = {"code": CODE, "code_id": "rc_" + "c" * 32, "expires_ms": 900}
    fake.answers["remote.device_remove"] = {"removed": True}
    return fake


@pytest.fixture
def host(gateway, monkeypatch):
    fake = link_of(OWNER, "owner")
    monkeypatch.setattr(gateway, "enabled", lambda ws: fake)
    monkeypatch.setattr(gateway, "link", lambda ws: fake)
    return fake


def writes(host):
    return [c for c in host.calls if c[2]]


def go(client, stub, token, method, path, headers=None):
    url = f"/api/w/{stub.ws}{path}"
    h = headers or hdr(token, method != "GET")
    return client.get(url, headers=h) if method == "GET" else client.post(url, json={}, headers=h)


def test_the_page_gets_the_closed_view_of_the_three_reads(client, token, stub, host):
    r = go(client, stub, token, "GET", "/remote")
    assert r.status_code == 200
    assert r.json() == {
        "on": True, "level": "work", "until_ms": 5000, "premises": "no", "off_reason": None, "isolated": False,
        "hub_mode": "selective",
        "devices": [{"device_id": DEV, "name": "Anna's laptop", "level_cap": "view", "added_ms": 1, "expires_ms": 9000,
                     "words": "one two three four five six"}],
        "pending": [{"pending": PENDING, "name": "Anna's phone", "words": "a b c d e f", "expires_ms": 77}],
        "sessions": [{"device": "Anna's laptop", "device_id": DEV}],
    }  # fmt: skip
    assert [c[0] for c in host.calls] == ["member.list", "remote.status", "remote.pending", "remote.sessions"]
    assert writes(host) == []


def test_a_view_of_nonsense_is_off_and_empty():
    assert view(None, None, None) == {
        "on": False, "level": None, "until_ms": None, "premises": None, "off_reason": None, "isolated": None,
        "hub_mode": "none", "devices": [], "pending": [], "sessions": [],
    }  # fmt: skip
    odd = view({"on": "yes", "level": "root", "devices": [{"device_id": "x"}]}, {"pending": [{"pending": "no"}]}, {})
    assert odd["on"] is False and odd["level"] is None and odd["devices"] == [] and odd["pending"] == []


def test_off_is_one_click_as_the_owner_with_a_new_idem(client, token, stub, host, caplog):
    with caplog.at_level(logging.WARNING, logger="orch_dashboard.security"):
        r = go(client, stub, token, "POST", "/remote/off")
    assert r.status_code == 200 and r.json() == {"changed": True}
    ((op, args, write, idem),) = writes(host)
    assert (op, args, write) == ("remote.off", {}, True) and idem.startswith("a_")
    assert any("turned off" in x.getMessage() and OWNER in x.getMessage() for x in caplog.records)


def test_a_device_is_removed_in_one_click_and_a_bad_id_reaches_nothing(client, token, stub, host):
    assert go(client, stub, token, "POST", f"/remote/devices/{DEV}/remove").json() == {"removed": True}
    ((op, args, write, _),) = writes(host)
    assert (op, args, write) == ("remote.device_remove", {"device_id": DEV}, True)
    host.calls.clear()
    assert go(client, stub, token, "POST", "/remote/devices/rd_xyz/remove").status_code in (400, 422)
    assert host.calls == []


def test_the_code_is_returned_once_and_never_logged(client, token, stub, host, caplog):
    with caplog.at_level(logging.DEBUG):
        r = go(client, stub, token, "POST", "/remote/code")
    assert r.status_code == 200 and r.json() == {"code": CODE, "expires_ms": 900}
    assert CODE not in caplog.text and "AAAA" not in caplog.text


def test_a_member_is_refused_before_any_write(client, token, stub, gateway, monkeypatch):
    member = link_of(MEMBER, "member")
    monkeypatch.setattr(gateway, "enabled", lambda ws: member)
    monkeypatch.setattr(gateway, "link", lambda ws: member)
    for method, path in ROUTES:
        r = go(client, stub, token, method, path)
        assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied", path
    assert writes(member) == []


def test_a_switch_session_reaches_nothing(client, auth, stub, host):
    switch = auth.open_switch(LOOPBACK, stub.ws)["token"]
    for method, path in ROUTES:
        r = go(client, stub, switch, method, path, hdr(switch, method != "GET"))
        assert r.status_code == 403, path
    assert host.calls == []


def test_every_route_is_refused_on_the_remote_listener(front, remote, stub, host):
    for method, path in ROUTES:
        r = go(front, stub, remote, method, path, bearer(remote, HTTPS))
        assert r.status_code == 403 and r.json()["error"]["code"] == "remote.refused", (method, path)
    assert host.calls == []


def test_a_remote_session_with_its_passkey_is_refused_too(front, auth, stub, host):
    passkey = auth.open_remote(credential_id="cred-1")["token"]
    for method, path in ROUTES:
        r = go(front, stub, passkey, method, path, bearer(passkey, HTTPS))
        assert r.status_code == 403 and r.json()["error"]["code"] == "remote.refused", (method, path)
    assert host.calls == []


def test_the_table_lists_the_routes_as_refused_on_remote():
    for method, path in ROUTES:
        assert routegate.TABLE[(method, "/api/w/{ws}" + path.replace(DEV, "{device_id}"))][0] == routegate.REFUSED


def test_turning_on_and_confirming_are_clicks_with_the_host_op_fixed_here():
    on = signing.RemoteSet(action="remote_set", level="work", premises="no", days=3)
    assert signing.host_call(on) == ("remote.set", {"level": "work", "premises": "no", "days": 3})
    forever = signing.RemoteSet(action="remote_set", level="view", premises="no", agents_risk=True)
    assert signing.host_call(forever) == ("remote.set", {"level": "view", "premises": "no", "agents_risk": True})
    add = signing.RemoteDeviceConfirm(action="remote_device_confirm", pending=PENDING, level_cap="view", days=7)
    assert signing.host_call(add) == (
        "remote.device_confirm", {"pending": PENDING, "level_cap": "view", "days": 7}
    )  # fmt: skip
    for bad in (
        {"action": "remote_set", "level": "root", "premises": "no", "days": 1},
        {"action": "remote_set", "level": "view", "premises": "maybe", "days": 1},
        {"action": "remote_set", "level": "view", "premises": "no", "days": 0},
        {"action": "remote_set", "level": "view", "premises": "no", "days": 1, "extra": 1},
    ):
        with pytest.raises(ValueError):
            signing.RemoteSet(**bad)


def test_the_prepared_event_must_be_exactly_what_was_asked():
    on = signing.RemoteSet(action="remote_set", level="work", premises="no", days=3)
    ok = {"type": "remote.changed", "on": True, "level": "work", "premises": "no", "until_ms": 9,
          "actor": {"kind": "person", "id": OWNER}}  # fmt: skip
    check = signing.expected(on, OWNER)
    assert check([ok])
    assert not check([{**ok, "level": "terminals"}])  # a stronger level than the one reviewed
    assert not check([{**ok, "until_ms": None}])  # "until I turn it off" when days were chosen
    assert not check([{**ok, "actor": {"kind": "person", "id": MEMBER}}])
    assert not check([ok, ok])
    forever = signing.RemoteSet(action="remote_set", level="view", premises="no")
    assert not signing.expected(forever, OWNER)([ok])


def test_a_remote_session_cannot_prepare_a_remote_access_change(front, auth, stub, host):
    passkey = auth.open_remote(credential_id="cred-1")["token"]
    body = {"action": "remote_set", "level": "view", "premises": "no", "days": 1}
    r = front.post(f"/api/w/{stub.ws}/click/prepare", json=body, headers=bearer(passkey, HTTPS))
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"
    assert host.calls == []
    body = {"action": "remote_device_confirm", "pending": PENDING, "level_cap": "view", "days": 1}
    r = front.post(f"/api/w/{stub.ws}/click/prepare", json=body, headers=bearer(passkey, HTTPS))
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"
