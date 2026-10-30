"""R2 T4 (#1590): the loopback page of remote access. Reads and the owner's plain clicks (off, code, remove a device)
go to the host as the session's person; turning ON and confirming a device are clicks with the backup key
(`signing.RemoteSet`, `RemoteDeviceConfirm`). Nothing here is reachable from the remote listener."""

# ruff: noqa: F811

import logging
import time

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
    fake.answers["remote.sessions"] = {"sessions": [{"stream_id": "5e" * 16, "device_id": DEV, "level": "work", "since_ms": 7}]}
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
        "sessions": [{"device": "Anna's laptop", "device_id": DEV, "stream_id": "5e" * 16, "level": "work", "since_ms": 7}],
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


def envelope(person=OWNER):
    return {"v": 1, "id": "e", "actor": {"kind": "person", "id": person}, "auth": "x", "hash_v": 1, "roster_v": 1,
            "based_on": None, "sig": "s"}  # fmt: skip


def changed_event(days=3, **over):
    now = int(time.time() * 1000)
    return {
        **envelope(), "type": "remote.changed", "on": True, "level": "work", "premises": "no",
        "until_ms": None if days is None else now + days * 86_400_000, "agents_risk": False, "hub_mode": "none",
        "remote_v": 0, "owner_proof": None, **over,
    }  # fmt: skip


def added_event(days=7, **over):
    from orch.hubclient import wire

    fp = bytes(range(32))
    return {
        **envelope(), "type": "remote.device_added", "device_id": DEV, "name": "Anna's phone", "person": OWNER,
        "presence_kind": "platform", "transport_pub": "t", "presence_pub": "p", "level_cap": "view",
        "expires_ms": int(time.time() * 1000) + days * 86_400_000, "fingerprint": fp.hex(), "code_id": "rc_" + "c" * 32,
        "remote_v": 0, "owner_proof": None, "_words": wire.fingerprint_words(fp), **over,
    }  # fmt: skip


def confirm_action():
    a = signing.RemoteDeviceConfirm(action="remote_device_confirm", pending=PENDING, level_cap="view", days=7)
    ev = added_event()
    a._seen = {"name": "Anna's phone", "words": ev.pop("_words")}  # noqa: SLF001
    return a


def test_the_prepared_remote_set_must_carry_exactly_every_field_chosen():
    on = signing.RemoteSet(action="remote_set", level="work", premises="no", days=3)
    check = signing.expected(on, OWNER)
    assert check([changed_event()])
    mutations = {
        "level": "terminals", "premises": "yes", "on": False, "type": "remote.disabled",
        "until_ms": None, "agents_risk": True, "hub_mode": "other", "remote_v": "1", "owner_proof": {"x": 1},
        "extra": 1, "actor": {"kind": "person", "id": MEMBER},
    }  # fmt: skip
    for field, value in mutations.items():
        assert not check([changed_event(**{field: value})]), field
    day = 86_400_000
    now = int(time.time() * 1000)
    for ms in (now + 4 * day, now + 2 * day, now + 3 * day + signing.REMOTE_SKEW_MS * 2):
        assert not check([changed_event(until_ms=ms)]), ms
    assert check([changed_event(until_ms=now + 3 * day + signing.REMOTE_SKEW_MS // 2)])
    gone = changed_event()
    del gone["agents_risk"]
    assert not check([gone]) and not check([changed_event(), changed_event()])
    assert not signing.expected(on, None)([changed_event()])


def test_remote_set_until_off_and_the_agents_risk_are_checked_both_ways():
    forever = signing.RemoteSet(action="remote_set", level="view", premises="no")
    assert signing.expected(forever, OWNER)([changed_event(days=None, level="view")])
    assert not signing.expected(forever, OWNER)([changed_event(days=3, level="view")])
    risky = signing.RemoteSet(action="remote_set", level="work", premises="no", days=3, agents_risk=True)
    assert signing.expected(risky, OWNER)([changed_event(agents_risk=True)])
    assert not signing.expected(risky, OWNER)([changed_event(agents_risk=False)])


def test_the_prepared_device_must_be_the_one_shown_with_exactly_the_choices():
    check = signing.expected(confirm_action(), OWNER)
    good = added_event()
    good.pop("_words")
    assert check([good])
    mutations = {
        "name": "Eve", "person": MEMBER, "level_cap": "terminals", "fingerprint": bytes(32).hex(),
        "device_id": "rd_xyz", "code_id": "rc_x", "presence_kind": "usb", "remote_v": "0", "owner_proof": {"x": 1},
        "extra": 1, "type": "remote.changed", "expires_ms": None,
    }  # fmt: skip
    for field, value in mutations.items():
        bad = {**good, field: value}
        assert not check([bad]), field
    day = 86_400_000
    now = int(time.time() * 1000)
    for ms in (now + 8 * day, now + 6 * day, now + 7 * day + signing.REMOTE_SKEW_MS * 2):
        assert not check([{**good, "expires_ms": ms}]), ms
    assert not check([good, good])
    unseen = signing.RemoteDeviceConfirm(action="remote_device_confirm", pending=PENDING, level_cap="view", days=7)
    assert not signing.expected(unseen, OWNER)([good])  # never looked up on the host: nothing matches


def test_a_remote_session_cannot_prepare_a_remote_access_change(front, auth, stub, host):
    passkey = auth.open_remote(credential_id="cred-1")["token"]
    body = {"action": "remote_set", "level": "view", "premises": "no", "days": 1}
    r = front.post(f"/api/w/{stub.ws}/click/prepare", json=body, headers=bearer(passkey, HTTPS))
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"
    assert host.calls == []
    body = {"action": "remote_device_confirm", "pending": PENDING, "level_cap": "view", "days": 1}
    r = front.post(f"/api/w/{stub.ws}/click/prepare", json=body, headers=bearer(passkey, HTTPS))
    assert r.status_code == 403 and r.json()["error"]["code"] == "op.not_allowed"
