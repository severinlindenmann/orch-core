import json
import os

import pytest

from addon_fixtures import make_addon
from orch import update
from orch.addons import discovery, manage, userfiles
from orch.core.events import Actor

DASH = Actor("human", "you", "dashboard")
ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def src(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "default_addons_dir", lambda: tmp_path / "no-defaults")
    monkeypatch.setattr(update, "core_source", lambda: None)
    s = make_addon(tmp_path / "src")
    manage.install(str(s), actor=DASH)
    return s


@pytest.fixture
def client(ws, src):
    from fastapi.testclient import TestClient
    from orch.dashboard.app import create_app
    c = TestClient(create_app(ws, "tok"))
    assert c.get("/?token=tok").status_code == 200
    return c


def test_update_all_asks_then_updates_and_trusts_again(client, ws, src):
    manage.trust_addon("hello-status", actor=DASH)
    manage.enable(ws.root, "hello-status", actor=DASH)
    data = json.loads((src / "orch-addon.json").read_text())
    (src / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0"}))
    ask = client.post("/workspace/addons/update-all", data={"ask": "1"}, headers=ORIGIN)
    assert "Update all" in ask.text and userfiles.registry_entries()["hello-status"]["version"] == "0.1.0"
    r = client.post("/workspace/addons/update-all", headers=ORIGIN, follow_redirects=False)
    assert "0.1.0+%E2%86%92+0.2.0" in r.headers["location"]
    assert userfiles.registry_entries()["hello-status"]["version"] == "0.2.0"
    assert userfiles.trust_state(discovery.find("hello-status")) == "trusted"
    assert userfiles.workspace_addons(ws.root)["hello-status"]["enabled"] is True
    again = client.post("/workspace/addons/update-all", headers=ORIGIN, follow_redirects=False)
    assert "Everything+is+up+to+date" in again.headers["location"]


def test_update_all_leaves_an_addon_with_new_permissions_for_review(client, ws, src):
    manage.trust_addon("hello-status", actor=DASH)
    data = json.loads((src / "orch-addon.json").read_text())
    (src / "orch-addon.json").write_text(json.dumps({**data, "version": "0.2.0", "binaries": ["git", "ls"]}))
    r = client.post("/workspace/addons/update-all", headers=ORIGIN, follow_redirects=False)
    assert "review+and+trust" in r.headers["location"]
    assert userfiles.trust_state(discovery.find("hello-status")) == "changed"


def test_update_all_refuses_a_foreign_origin(client):
    r = client.post("/workspace/addons/update-all", headers={"origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code in (400, 403)


def _fake_claude(folder, monkeypatch, body):
    claude = folder / "claude"
    folder.mkdir(parents=True)
    claude.write_text(f"#!/bin/sh\n{body}\n")
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(folder))


def test_plugin_refresh_reports_what_claude_said(tmp_path, monkeypatch):
    _fake_claude(tmp_path / "ok", monkeypatch, 'echo "updated to abc"')
    assert update.refresh_plugin() == "Claude plugin: updated to abc. Restart Claude Code sessions to load it."
    _fake_claude(tmp_path / "bad", monkeypatch, 'echo boom >&2; exit 3')
    assert update.refresh_plugin() == "the Claude plugin was not refreshed: boom"
    monkeypatch.setenv("PATH", os.fspath(tmp_path / "empty"))
    assert "no claude CLI on PATH" in update.refresh_plugin()
