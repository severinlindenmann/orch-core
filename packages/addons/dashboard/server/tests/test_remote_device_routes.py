"""R3 T4 (#1591): the routes around a remote session. The End button (this computer only), the presence routes (a
stream only) and turning remote access off from an owner's own session."""

# ruff: noqa: F811

from conftest import PORT  # noqa: F401
from device_kit import STREAM, app, click_start, fake, passkey_store, stream_client, ws  # noqa: F401
from remote_kit import front, local, loop  # noqa: F401
from test_market_routes import Host, hdr
from test_remote_access_routes import MEMBER, OWNER, link_of

from orch_dashboard.click_signer import STREAM_ARGS

SID = "5e" * 16


def end(client, ws, token, sid=SID):
    return client.post(f"/api/w/{ws}/remote/sessions/{sid}/end", json={}, headers=hdr(token, True))


def test_end_button_ends_session(client, token, stub, gateway, monkeypatch):
    host = link_of(OWNER, "owner")
    host.answers["remote.session_end"] = {"ended": True}
    monkeypatch.setattr(gateway, "enabled", lambda ws: host)
    monkeypatch.setattr(gateway, "link", lambda ws: host)
    r = end(client, stub.ws, token)
    assert r.status_code == 200 and r.json() == {"ended": True}
    ((op, args, write, idem),) = [c for c in host.calls if c[2]]
    assert (op, args, write) == ("remote.session_end", {"stream_id": SID}, True) and idem.startswith("a_")


def test_end_is_for_an_owner_on_this_computer_and_takes_a_stream_id_only(client, token, stub, gateway, monkeypatch):
    member = link_of(MEMBER, "member")
    monkeypatch.setattr(gateway, "enabled", lambda ws: member)
    monkeypatch.setattr(gateway, "link", lambda ws: member)
    r = end(client, stub.ws, token)
    assert r.status_code == 403 and r.json()["error"]["code"] == "role.denied"
    assert [c for c in member.calls if c[2]] == []
    owner = link_of(OWNER, "owner")
    monkeypatch.setattr(gateway, "enabled", lambda ws: owner)
    monkeypatch.setattr(gateway, "link", lambda ws: owner)
    assert end(client, stub.ws, token, "nope").status_code in (400, 422)
    assert [c for c in owner.calls if c[2]] == []


def test_end_is_refused_inside_a_stream_and_on_the_front(app, auth, ws, fake):
    client = stream_client(app, auth, ws)[0]
    r = client.post(f"/api/w/{ws}/remote/sessions/{SID}/end", json={})
    assert r.status_code == 403 and r.json()["error"]["code"] == "remote.refused"


def test_the_presence_routes_pass_the_signer_the_stream_and_nothing_else(app, auth, ws, fake):
    client = stream_client(app, auth, ws)[0]
    fake.answers["presence_nonce"] = {"nonce": "N" * 43}
    r = client.post(f"/api/w/{ws}/remote/presence/nonce", json={})
    assert r.status_code == 200 and r.json() == {"nonce": "N" * 43}
    op, args = fake.calls[-1]
    assert op == "presence_nonce" and args == {"workspace_id": ws, "remote": True, "stream": STREAM}

    digest = "sha256:" + "ab" * 32
    fake.answers["presence"] = {"ok": True, "n": 1}
    body = {"nonce": "N" * 43, "digests": [digest], "assertion": {"signature": "s"}}
    r = client.post(f"/api/w/{ws}/remote/presence", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True, "n": 1}
    op, args = fake.calls[-1]
    assert op == "presence" and args["digests"] == [digest] and args["stream"] == STREAM and args["remote"] is True
    # a body that names the stream or the person is no body
    for extra in ({"stream": "x"}, {"remote": True}, {"person": "p"}):
        assert client.post(f"/api/w/{ws}/remote/presence", json={**body, **extra}).status_code == 400
    assert client.post(f"/api/w/{ws}/remote/presence", json={**body, "digests": []}).status_code == 400
    assert STREAM_ARGS == {"remote", "stream", "stepup"}


def test_the_presence_routes_do_not_exist_outside_a_stream(client, token, stub):
    for path in ("/remote/presence/nonce", "/remote/presence"):
        body = {"nonce": "N", "digests": ["sha256:" + "ab" * 32], "assertion": {}} if path == "/remote/presence" else {}
        r = client.post(f"/api/w/{stub.ws}{path}", json=body, headers=hdr(token, True))
        assert r.status_code == 403, path  # loopback: the handler's own wall (no stream)


def test_off_from_an_owners_own_session_names_the_stream_only(app, auth, ws, gateway, monkeypatch):
    host = Host()
    host.answers["remote.off"] = {"changed": True}
    monkeypatch.setattr(gateway, "enabled", lambda w: host)
    for level in ("view", "work", "terminals"):  # turning off needs no more than turning on (D84)
        host.calls.clear()
        client = stream_client(app, auth, ws, level=level, stream=level[:2] * 16)[0]
        r = client.post(f"/api/w/{ws}/remote/off", json={})
        assert r.status_code == 200 and r.json() == {"changed": True}, level
        ((op, args, write, idem),) = host.calls
        assert (op, args, write) == ("remote.off", {"stream_id": level[:2] * 16}, True) and idem
