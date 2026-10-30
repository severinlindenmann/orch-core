"""HUB-WS-09: the workspace profile routes. Owner only, a full session, a closed shape, no signing, nothing logged."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orch_dashboard import profile_routes
from orch_dashboard.errors import HostError, host_response

WS = "a" * 32
PROFILE = {
    "listed": True, "tags": ["billing"], "handles": ["invoices"], "not_handled": [], "accepts": ["question"],
    "description": "Invoices and payments.", "contact": "Dana", "languages": "en, de", "hours": "9-17",
}  # fmt: skip
OFF = {**PROFILE, "listed": False}


class Link:
    def __init__(self, role="owner", profile=None):
        self.role, self.profile, self.calls = role, dict(profile or OFF), []

    def call(self, op, args=None, **kw):
        self.calls.append((op, args, kw))
        if op == "member.list":
            return {"members": [{"person": "p1", "role": self.role}]}
        assert op == "hub.profile"
        if args:
            self.profile = args["profile"]
            return {"profile": args["profile"], "pushed": False, "secret": "x"}
        return {"profile": {**self.profile, "extra": 1}}


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
    profile_routes.register(app, Gw(link))

    @app.middleware("http")
    async def token(request, call_next):
        request.state.token = "t"
        return await call_next(request)

    @app.exception_handler(HostError)
    def _(request, exc):
        return host_response(exc)

    return TestClient(app)


URL = f"/api/w/{WS}/hub/profile"


def test_the_owner_reads_a_closed_shape():
    got = client(Link(profile=PROFILE)).get(URL).json()
    assert got == {**PROFILE, "pushed": False} and "extra" not in got


def test_the_owner_saves_with_an_idem_and_gets_pushed_false_without_a_hub(caplog):
    link = Link()
    with caplog.at_level(logging.WARNING, logger="orch.dashboard.profile"):
        got = client(link).put(URL, json=PROFILE)
    assert got.status_code == 200 and got.json() == {**PROFILE, "pushed": False}  # `secret` of the host answer is gone
    op, args, kw = link.calls[-1]
    assert op == "hub.profile" and args == {"profile": PROFILE} and kw["write"] is True and kw["idem"]
    assert "Invoices" not in caplog.text  # the text is never logged


@pytest.mark.parametrize("role", ["admin", "member", "reader"])
def test_only_an_owner_reads_or_changes_it(role):
    link = Link(role=role)
    c = client(link)
    assert c.get(URL).status_code == 403 and c.put(URL, json=PROFILE).status_code == 403
    assert not any(call[0] == "hub.profile" for call in link.calls)


def test_a_switch_session_is_refused_before_the_host_is_asked():
    link = Link()
    c = client(link, full=False)
    assert c.get(URL).status_code != 200 and c.put(URL, json=PROFILE).status_code != 200
    assert link.calls == []


def test_listing_on_from_a_remote_session_needs_this_computer_but_other_edits_do_not():
    link = Link(profile=OFF)
    c = client(link, kind="remote")
    res = c.put(URL, json=PROFILE)
    assert res.status_code == 403 and res.json()["error"]["code"] == "passkey.stepup_required"
    assert not any(call[1] for call in link.calls if call[0] == "hub.profile")
    assert c.put(URL, json={**OFF, "hours": "8-12"}).status_code == 200  # not listed, stays not listed
    on = Link(profile=PROFILE)
    assert client(on, kind="remote").put(URL, json={**PROFILE, "hours": "8-12"}).status_code == 200  # already listed


@pytest.mark.parametrize(
    "bad",
    [{"extra": 1}, {"tags": ["a"] * 9}, {"description": "x" * 301}, {"listed": "yes"}, {"hours": "x" * 41}],
)
def test_a_body_outside_the_closed_schema_never_reaches_the_host(bad):
    link = Link()
    assert client(link).put(URL, json={**PROFILE, **bad}).status_code == 422
    assert not any(call[1] for call in link.calls if call[0] == "hub.profile")


def test_hidden_characters_in_host_text_are_withheld():
    link = Link(profile={**PROFILE, "description": "a‮b", "tags": ["ok", "x​y"]})
    got = client(link).get(URL).json()
    assert got["description"] == "" and got["tags"] == ["ok"]


@pytest.mark.parametrize(
    "bad",
    [{"description": "a\u202eb"}, {"contact": "x\u200by"}, {"languages": "e\u2066n"}, {"hours": "9\x0017"},
     {"tags": ["ok", "a\u200db"]}, {"handles": ["x\u202ey"]}, {"not_handled": ["\u2060"]}, {"accepts": ["q\u00adr"]}],
)
def test_hidden_or_bidi_characters_are_refused_and_nothing_is_stored(bad):
    link = Link()
    assert client(link).put(URL, json={**PROFILE, **bad}).status_code == 422
    assert not any(call[1] for call in link.calls if call[0] == "hub.profile")
    assert link.profile == OFF
