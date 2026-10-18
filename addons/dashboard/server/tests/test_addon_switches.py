"""Settings > Add-ons (R2-2): the typed hand-off actions and the rules for a switch that cannot move."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard.addon_switches import busy, needs
from orch_dashboard.handoff import AddonDisable, AddonGrant, PrepareBody, expected, host_call


def parse(body):
    return TypeAdapter(PrepareBody).validate_python(body)


def test_turning_an_addon_on_or_off_is_a_typed_action_for_the_shipped_addons_only():
    on = parse({"action": "addon_grant", "name": "terminals"})
    off = parse({"action": "addon_disable", "name": "worktrees"})
    assert isinstance(on, AddonGrant) and isinstance(off, AddonDisable)
    assert host_call(on) == ("addon.grant", {"name": "terminals"})
    assert host_call(off) == ("addon.disable", {"name": "worktrees"})
    for bad in ("dashboard", "../x", "echo", "", "Terminals"):  # the dashboard stays on; no other package is offered
        with pytest.raises(ValidationError):
            parse({"action": "addon_grant", "name": bad})
        with pytest.raises(ValidationError):
            parse({"action": "addon_disable", "name": bad})
    with pytest.raises(ValidationError):
        parse({"action": "addon_grant", "name": "terminals", "extra": 1})


def test_the_prepared_event_must_name_exactly_that_addon():
    on = expected(parse({"action": "addon_grant", "name": "terminals"}))
    assert on([{"type": "addon.granted", "name": "terminals"}])
    assert not on([{"type": "addon.granted", "name": "start-agent"}])
    assert not on([{"type": "addon.disabled", "name": "terminals"}])
    assert not on([{"type": "addon.granted", "name": "terminals"}, {"type": "addon.granted", "name": "worktrees"}])
    off = expected(parse({"action": "addon_disable", "name": "terminals"}))
    assert off([{"type": "addon.disabled", "name": "terminals"}])
    assert not off([{"type": "addon.purged", "name": "terminals"}])


def test_an_addon_with_running_work_cannot_be_turned_off():
    runs = [{"kind": "agent", "cwd": {"kind": "worktree"}}, {"kind": "shell", "cwd": {"kind": "workspace"}}]
    assert "an agent of yours is running" in busy("start-agent", runs)
    assert "a terminal of yours is running" in busy("terminals", runs)
    assert "worktree" in busy("worktrees", runs)
    assert busy("start-agent", [{"kind": "shell"}]) is None
    assert busy("terminals", []) is None
    assert busy("terminals", None)  # unknown counts as busy: fail closed


def test_an_addon_that_needs_dashboard_execution_says_what_to_run():
    off = {"enabled": False, "terminals": False, "connected": False}
    assert "orch runner enable" in needs("start-agent", off)
    assert "orch runner enable" in needs("terminals", off)
    assert needs("worktrees", off) is None
    assert needs("start-agent", {**off, "enabled": True}) is None
    assert "Terminals are not turned on" in needs("terminals", {**off, "enabled": True})
    assert needs("terminals", {**off, "enabled": True, "terminals": True}) is None
    assert needs("start-agent", None)  # the host says nothing: not offered
