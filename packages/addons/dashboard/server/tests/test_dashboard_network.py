"""R6 #1594: the network (proxy) routes. Owner, a full session on this computer, a closed shape, the password never
returned or logged."""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orch_dashboard import network_routes
from orch_dashboard.errors import HostError, host_response

WS = "a" * 32
URL = f"/api/w/{WS}/hub/network"


class Link:
    def __init__(self, role="owner"):
        self.role, self.calls = role, []

    def call(self, op, args=None, **kw):
        self.calls.append((op, args, kw))
        if op == "member.list":
            return {"members": [{"person": "p1", "role": self.role}]}
        if op == "hub.network_set":
            return {"proxy": args["proxy"], "user": args.get("user"), "has_password": "password" in args,
                    "broken": False, "password": "LEAK"}  # fmt: skip
        return {"proxy": None, "user": None, "has_password": False, "broken": False}


class Gw:
    def __init__(self, link):
        self.link = link

    def enabled(self, ws):
        return self.link


class Auth:
    def __init__(self, kind="local", full=True):
        self.kind, self.full = kind, full

    def login_kind(self, token):
        return self.kind

    def switch_workspace(self, token):
        return None if self.full else WS

    def listener_of(self, token):
        return "loopback"

    def passkey_session(self, token):
        return True


def client(link, kind="local", full=True):
    app = FastAPI()
    app.state.auth = Auth(kind, full)
    app.state.person_of = lambda ws: "p1"
    network_routes.register(app, Gw(link))

    @app.middleware("http")
    async def token(request, call_next):
        request.state.token = "t"
        return await call_next(request)

    @app.exception_handler(HostError)
    def _(request, exc):
        return host_response(exc)

    return TestClient(app)


def test_read_and_set_with_a_closed_answer(caplog):
    link = Link()
    c = client(link)
    assert c.get(URL).json() == {"proxy": None, "user": None, "has_password": False, "broken": False}
    with caplog.at_level(logging.WARNING, logger="orch.dashboard.network"):
        r = c.put(URL, json={"proxy": "https://p.office:3128", "user": "a", "password": "s3cret"})
    assert r.status_code == 200 and "LEAK" not in r.text and "s3cret" not in r.text
    assert r.json()["has_password"] is True
    op, args, kw = link.calls[-1]
    assert op == "hub.network_set" and args["password"] == "s3cret" and kw["write"] and kw["idem"]
    assert "s3cret" not in caplog.text


def test_clearing_sends_only_the_proxy():
    link = Link()
    assert client(link).put(URL, json={"proxy": None}).status_code == 200
    assert link.calls[-1][1] == {"proxy": None}


def test_bad_shapes_never_echo_the_input():
    link = Link()
    r = client(link).put(URL, json={"proxy": "http://p:1", "password": 5, "extra": "s3cret"})
    assert r.status_code >= 400 and "s3cret" not in r.text
    assert all(c[0] == "member.list" or c[0] != "hub.network_set" for c in link.calls)


def test_not_an_owner_not_remote_not_a_switch_session():
    assert client(Link("member")).get(URL).status_code == 403
    for kw in ({"kind": "remote"}, {"full": False}):
        link = Link()
        r = client(link, **kw).put(URL, json={"proxy": "http://p.office:3128"})
        assert r.status_code >= 400 and not [c for c in link.calls if c[0] == "hub.network_set"]
