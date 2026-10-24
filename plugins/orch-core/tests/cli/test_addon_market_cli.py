"""V3 G3: ``orch addon install | update | remove``: who may run what before the host is asked, and what is printed."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from orch.cli import addon_market as am
from orch.cli import publish


def run(argv, env=None, *, as_json=True):
    out = io.StringIO()
    code = am.main([*argv, *(["--json"] if as_json else [])], env=env or {}, out=out, err=io.StringIO())
    return code, out.getvalue()


@pytest.fixture
def host(monkeypatch):
    log = SimpleNamespace(called=[], connected=[], reply={})
    monkeypatch.setattr(publish, "_target", lambda env: object())

    def call(t, op, args, env):
        log.called.append((op, args))
        return log.reply.get(op, {"addons": []})

    monkeypatch.setattr(publish, "_call", call)
    monkeypatch.setattr(am, "_connect", lambda *a: log.connected.append(a) or (_ for _ in ()).throw(OSError("no host")))
    return log


@pytest.mark.parametrize(
    "argv",
    [["install", "standup"], ["install", "standup", "--from", "https://github.com/a/b"], ["update", "standup"],
     ["remove", "standup"]],
)  # fmt: skip
@pytest.mark.parametrize("env", [{"ORCH_GRANT": "g"}, {"ORCH_SESSION": "s"}])
def test_an_agent_is_refused_before_the_host_is_asked(host, argv, env):
    code, out = run(argv, env)
    assert code == 3 and json.loads(out)["code"] == "market.actor_refused"
    assert host.called == [] and host.connected == []


def test_an_agent_may_check_for_updates(host):
    host.reply["market.installed"] = {"addons": [{"name": "standup", "version": "1.0.0", "available": None}]}
    code, out = run(["update", "--check"], {"ORCH_GRANT": "g"})
    assert code == 0 and json.loads(out)["data"]["addons"][0]["name"] == "standup"
    assert host.called == [("market.installed", {})]
    code, out = run(["update", "standup", "--check"], {"ORCH_SESSION": "s"})
    assert code == 0 and json.loads(out)["data"]["addons"][0]["name"] == "standup"
    code, out = run(["update", "nothing", "--check"])
    assert code == 3 and json.loads(out)["code"] == "market.not_installed"


def test_check_text_marks_a_newer_and_a_revoked_version(host):
    host.reply["market.installed"] = {
        "addons": [
            {"name": "a", "version": "1.0.0", "state": "enabled", "origin": "u",
             "available": {"version": "1.1.0", "capabilities_added": ["pty"], "revoked": False}},
            {"name": "b", "version": "1.0.0", "state": "disabled", "origin": "u",
             "available": {"version": "1.0.0", "capabilities_added": [], "revoked": True}},
        ]
    }  # fmt: skip
    code, out = run(["update", "--check"], as_json=False)
    assert code == 0 and "newer: 1.1.0 (adds pty)" in out and "REVOKED" in out


@pytest.mark.parametrize(
    "argv",
    [["install"], ["install", "a", "b"], ["update"], ["remove"], ["remove", "a", "--check"], ["update", "a", "--from", "u"]],
)  # fmt: skip
def test_usage_errors_touch_nothing(host, argv):
    code, out = run(argv)
    assert code == 2 and json.loads(out)["code"] == "invalid.input"
    assert host.called == [] and host.connected == []


def test_a_person_without_a_host_gets_a_plain_error(host):
    code, out = run(["install", "standup"])
    assert code == 9 and json.loads(out)["code"] == "host.unavailable"
