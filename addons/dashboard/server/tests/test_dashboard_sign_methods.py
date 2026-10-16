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
    assert "sign_methods" not in got  # never set: every way is allowed (the model judges none); nothing is invented


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
