"""B6 (owner decision F): the workspace's allowed ways to approve (`sign_methods`, a signed workspace setting) and the
person's own default (`sign_default`, kept on this machine, not signed)."""

import time

import pytest
from team import add_member

from orch_dashboard import signprefs


@pytest.fixture
def owner(p1, tmp_path, monkeypatch):
    p1.clock[0] = int(time.time())
    monkeypatch.setenv("ORCH_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state" / "dashboard").mkdir(parents=True, mode=0o700)
    return p1


def handoff(world, body):
    return world.post(f"/api/w/{world.stub.ws}/handoff", {**body})


def test_the_settings_read_says_which_ways_are_allowed(worlds, owner):
    world = worlds(owner.owner.ref)
    got = world.get(f"/api/w/{world.stub.ws}/settings").json()
    # never set: the host reports null (every way allowed, the model judges none) and the dashboard passes the null
    # through, so the UI can tell "not set" from an older core that says nothing (absent)
    assert "sign_methods" in got and got["sign_methods"] is None


def test_sign_methods_is_a_typed_owner_setting_and_terminal_is_always_there(worlds, owner):
    world = worlds(owner.owner.ref)
    ok = handoff(world, {"action": "settings_set", "sign_methods": ["terminal", "click", "phone"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["events"][0]["set"] == {"sign_methods": ["terminal", "click", "phone"]}
    for bad in (
        ["click"],  # terminal is always allowed
        [],
        ["terminal", "touchid"],  # reserved, not selectable yet
        ["terminal", "terminal"],
        ["terminal", "fingerprint"],
    ):
        res = handoff(world, {"action": "settings_set", "sign_methods": bad})
        assert res.status_code == 400 and res.json()["error"]["code"] == "invalid.input", bad


def test_a_default_is_per_person_and_only_for_an_allowed_way(worlds, owner):
    world = worlds(owner.owner.ref)
    url = f"/api/w/{world.stub.ws}/me/sign_default"
    every = ["terminal", "click", "phone"]  # never set: every way is allowed
    assert world.get(url).json() == {"method": "terminal", "is_default": True, "allowed": every}
    owner.tick()
    owner.store.append(
        owner.person_event(owner.owner, "workspace", "settings.changed", set={"sign_methods": ["terminal"]}),
        log="workspace",
    )
    assert world.get(url).json()["allowed"] == ["terminal"]
    refused = world.put(url, {"method": "click"})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid.input"
    assert world.put(url, {"method": "terminal", "fallback": "terminal"}).status_code in (400, 422)
    # allow one click in the workspace (the setting is applied as the signed event would apply it)
    owner.tick()
    owner.store.append(
        owner.person_event(owner.owner, "workspace", "settings.changed", set={"sign_methods": ["terminal", "click"]}),
        log="workspace",
    )
    got = world.put(url, {"method": "click", "fallback": "terminal"}).json()
    assert got == {"method": "click", "fallback": "terminal", "is_default": False, "allowed": ["terminal", "click"]}
    assert world.get(url).json() == got
    # another person's default is their own
    other = add_member(owner, "Tom", "member")
    assert signprefs.load(other.ref, world.stub.ws) is None
    assert signprefs.load(owner.owner.ref, world.stub.ws) == {"method": "click", "fallback": "terminal"}


def test_a_default_that_stopped_being_allowed_is_not_used(worlds, owner):
    world = worlds(owner.owner.ref)
    url = f"/api/w/{world.stub.ws}/me/sign_default"
    owner.tick()
    owner.store.append(
        owner.person_event(owner.owner, "workspace", "settings.changed", set={"sign_methods": ["terminal", "click"]}),
        log="workspace",
    )
    assert world.put(url, {"method": "click"}).status_code == 200
    owner.tick()
    owner.store.append(
        owner.person_event(owner.owner, "workspace", "settings.changed", set={"sign_methods": ["terminal"]}),
        log="workspace",
    )
    assert world.get(url).json() == {"method": "terminal", "is_default": True, "allowed": ["terminal"]}


def test_a_risk_notice_acknowledgement_is_per_person_and_workspace(worlds, owner):
    from orch_dashboard import riskack

    world = worlds(owner.owner.ref)
    url = f"/api/w/{world.stub.ws}/me/risk_notices"
    assert world.get(url).json() == {"acknowledged": {}}
    assert world.post(url, {"notice": "anything"}).status_code in (400, 422)
    assert world.post(url, {"notice": "start_agent"}).json() == {"acknowledged": {"start_agent": True}}
    assert world.get(url).json() == {"acknowledged": {"start_agent": True}}
    other = add_member(owner, "Tom", "member")
    assert riskack.load(other.ref, world.stub.ws) == {}
    assert riskack.load(owner.owner.ref, "w" * 32) == {}


def test_login_shell_is_its_own_notice_and_the_sentence_matches_core(worlds, owner):
    from pathlib import Path

    from orch.host.launch import LOGIN_NOTICE

    world = worlds(owner.owner.ref)
    url = f"/api/w/{world.stub.ws}/me/risk_notices"
    assert world.post(url, {"notice": "login_shell"}).json() == {"acknowledged": {"login_shell": True}}
    both = world.post(url, {"notice": "start_agent"}).json()
    assert both == {"acknowledged": {"login_shell": True, "start_agent": True}}
    # the page matches the host's sentence exactly; this pins both copies together
    ts = (Path(__file__).parents[2] / "src" / "api" / "agents.ts").read_text(encoding="utf-8")
    assert f"'{LOGIN_NOTICE}'" in ts


def test_grant_notice_is_its_own_key(worlds, owner):
    world = worlds(owner.owner.ref)
    url = f"/api/w/{world.stub.ws}/me/risk_notices"
    assert world.post(url, {"notice": "grant"}).json() == {"acknowledged": {"grant": True}}


def test_one_click_on_makes_click_the_effective_default_until_a_choice_is_stored(worlds, owner, monkeypatch):
    from orch_dashboard.gateway import Gateway

    world = worlds(owner.owner.ref)
    url = f"/api/w/{world.stub.ws}/me/sign_default"
    every = ["terminal", "click", "phone"]
    assert world.get(url).json()["method"] == "terminal"  # one click off: unchanged
    monkeypatch.setattr(Gateway, "one_path_is_click", lambda self, ws: True)
    assert world.get(url).json() == {"method": "click", "is_default": True, "allowed": every}
    # a stored choice always wins
    assert world.put(url, {"method": "terminal"}).status_code == 200
    assert world.get(url).json() == {"method": "terminal", "is_default": False, "allowed": every}
    # the workspace not allowing click: terminal, even with the flag on (the allowed list is the rule)
    owner.tick()
    owner.store.append(
        owner.person_event(owner.owner, "workspace", "settings.changed", set={"sign_methods": ["terminal"]}),
        log="workspace",
    )
    assert world.put(url, {"method": "terminal"}).status_code == 200  # (stored choice is dropped below)
    import os

    os.remove(signprefs._path())
    assert world.get(url).json() == {"method": "terminal", "is_default": True, "allowed": ["terminal"]}
