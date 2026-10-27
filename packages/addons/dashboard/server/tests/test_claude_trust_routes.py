"""The Claude Code consent switch routes, and the records auto-on click (small self-contained apps)."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from orch.runner import claude_trust
from orch.setup import claude_folder

from orch_dashboard import addon_routes, claude_trust_routes
from orch_dashboard.errors import HostError, host_response
from orch_dashboard.policy import LOOPBACK


class FakeAuth:
    def __init__(self, kind="local", listener=LOOPBACK):
        self.kind, self.listener = kind, listener

    def login_kind(self, token):
        return self.kind

    def listener_of(self, token):
        return self.listener


class FakeLink:
    welcome = {"ops": {}}

    def supports(self, op, cap):
        return True

    def call(self, op, args=None):
        return {}


class FakeGateway:
    state = None
    run = None

    def __init__(self, folder):
        self.folder = folder

    def workspace_folder(self, ws):
        return (self.folder, None)

    def enabled(self, ws):
        return FakeLink()


def build(tmp_path, auth=None):
    app = FastAPI()
    gateway = FakeGateway(tmp_path)

    @app.middleware("http")
    async def token(request: Request, call_next):
        request.state.token = "t"
        return await call_next(request)

    @app.exception_handler(HostError)
    async def host_error(request, exc):
        return host_response(exc)

    claude_trust_routes.register(app, gateway, auth or FakeAuth())
    addon_routes.register(app, gateway)
    return TestClient(app)


def test_get_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_trust, "_consented", lambda root: True)
    monkeypatch.setattr(claude_folder, "offered", lambda *a, **k: True)
    monkeypatch.setattr(claude_folder, "refusal", lambda *a, **k: None)
    monkeypatch.setattr(claude_folder, "parent_config", lambda *a, **k: None)
    res = build(tmp_path).get("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust")
    assert res.status_code == 200, res.text
    assert res.json() == {"available": True, "on": True, "offered": True, "why_not": "", "parent_config": ""}


def test_post_grants(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(claude_folder, "grant_consent", lambda env, project, allow_parent: seen.append(allow_parent))
    res = build(tmp_path).post("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust", json={"allow_parent": True})
    assert res.status_code == 200, res.text
    assert res.json() == {"on": True} and seen == [True]


def test_post_refusal_returns_the_plain_reason(tmp_path, monkeypatch):
    reason = "This folder is not one Claude Code may be told to trust."
    monkeypatch.setattr(claude_folder, "grant_consent", lambda env, project, allow_parent: reason)
    res = build(tmp_path).post("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust", json={"allow_parent": False})
    assert res.status_code >= 400
    assert reason in res.text


def test_post_body_is_strict(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_folder, "grant_consent", lambda *a, **k: None)
    client = build(tmp_path)
    assert client.post("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust", json={"allow_parent": "yes"}).status_code == 422
    assert client.post("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust", json={"allow_parent": True, "x": 1}).status_code == 422


@pytest.mark.parametrize("auth", [FakeAuth(kind="switch"), FakeAuth(listener="front")])
def test_a_non_local_session_is_refused(tmp_path, monkeypatch, auth):
    called = []
    monkeypatch.setattr(claude_folder, "grant_consent", lambda *a, **k: called.append(1))
    monkeypatch.setattr(claude_trust, "_consented", lambda root: called.append(1))
    client = build(tmp_path, auth)
    assert client.get("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust").status_code >= 400
    assert client.post("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/claude-trust", json={"allow_parent": True}).status_code >= 400
    assert called == []


def test_records_auto_on_runs_the_child_action(tmp_path, monkeypatch):
    seen = []

    def run(self, ws, **kw):
        seen.append((ws, kw.get("action"), kw.get("url")))
        return {"ok": True}

    monkeypatch.setattr(addon_routes.RecordsChild, "run", run)
    res = build(tmp_path).post("/api/w/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/records/auto-on", json={})
    assert res.status_code == 200, res.text
    assert seen == [("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "auto-on", None)]
