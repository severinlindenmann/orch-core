"""Settings > Add-ons (R2-2): the typed hand-off actions and the rules for a switch that cannot move."""

import pytest
from pydantic import TypeAdapter, ValidationError

from orch_dashboard import handoff
from orch_dashboard.addon_switches import busy, needs
from orch_dashboard.errors import HostError
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


def test_publish_is_a_shipped_addon_with_no_machine_precondition_and_plain_words():
    """P5: Publish is switched like the others (a signed addon.grant), needs no dashboard execution and holds no run."""
    from orch.setup import facts

    assert isinstance(parse({"action": "addon_grant", "name": "publish"}), AddonGrant)
    assert host_call(parse({"action": "addon_disable", "name": "publish"})) == ("addon.disable", {"name": "publish"})
    off = {"enabled": False, "terminals": False, "connected": False}
    assert needs("publish", off) is None and needs("publish", None) is None
    assert busy("publish", [{"kind": "agent", "cwd": {"kind": "worktree"}}]) is None
    listed = {a.id: a for a in facts.catalog(optional=True)}
    assert "publish" in listed and listed["publish"].title == "Publish"
    assert "nothing leaves this computer until you sign" in listed["publish"].desc.lower()
    assert "publish" not in {a.id for a in facts.catalog()}  # the setup wizard does not offer or nag about it


# ---- A-1: on, off, on is three intents; a double click is one; asking for what already is is said in words ----------
class _SwitchLink:
    """A host that keeps one add-on's state and its workspace-log events, like `addon.list` and `activity.list`."""

    welcome = {"peer": {"person": "p_me"}, "ops": {"addon.grant": 1, "addon.disable": 1}}

    def __init__(self):
        self.state, self.events = "not_granted", []

    def supports(self, op, cap):
        return False  # no run.list: nothing is running

    def call(self, op, args=None):
        if op == "addon.list":
            return {"addons": [{"name": "terminals", "state": self.state}, {"name": "worktrees", "state": "active"}]}
        assert op == "activity.list" and args["scope"] == "workspace"
        assert set(args["types"]) == {"addon.granted", "addon.disabled"}
        return {"items": [dict(e) for e in reversed(self.events)], "next_page": None}

    def switch(self, on):  # what a signature does to the host
        self.state = "active" if on else "disabled"
        n = len(self.events) + 1
        typ = "addon.granted" if on else "addon.disabled"
        self.events.append({"id": f"{n}.e", "type": typ, "data": {"name": "terminals"}})
        self.events.append({"id": f"{n}.x", "type": "addon.granted", "data": {"name": "worktrees"}})  # another one


def _plan(gateway, stub, link, monkeypatch, action, name="terminals"):
    monkeypatch.setattr(gateway, "link", lambda ws: link)
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: {})
    return handoff.plan(gateway, stub.ws, parse({"action": action, "name": name}))


def test_on_off_on_is_three_requests_and_a_double_click_is_one(gateway, stub, monkeypatch):
    link = _SwitchLink()
    on1 = _plan(gateway, stub, link, monkeypatch, "addon_grant")
    assert _plan(gateway, stub, link, monkeypatch, "addon_grant").idem(stub.ws) == on1.idem(stub.ws)  # a double click
    link.switch(True)
    off = _plan(gateway, stub, link, monkeypatch, "addon_disable")
    assert off.idem(stub.ws) == _plan(gateway, stub, link, monkeypatch, "addon_disable").idem(stub.ws)
    link.switch(False)
    on2 = _plan(gateway, stub, link, monkeypatch, "addon_grant")
    # the second "on" has the same op, args and person as the first: only the latest switch tells them apart
    assert on2.op == on1.op and on2.args == on1.args
    assert len({on1.idem(stub.ws), off.idem(stub.ws), on2.idem(stub.ws)}) == 3
    link.switch(True)
    off2 = _plan(gateway, stub, link, monkeypatch, "addon_disable")
    assert off2.idem(stub.ws) != off.idem(stub.ws)  # off, on, off: the second off is its own request too


def test_another_addons_switch_does_not_move_this_intent(gateway, stub, monkeypatch):
    link = _SwitchLink()
    before = _plan(gateway, stub, link, monkeypatch, "addon_grant").idem(stub.ws)
    link.events.append({"id": "9.e", "type": "addon.granted", "data": {"name": "worktrees"}})
    assert _plan(gateway, stub, link, monkeypatch, "addon_grant").idem(stub.ws) == before


def test_asking_for_what_already_is_is_said_in_words_and_never_prepared(gateway, stub, monkeypatch):
    link = _SwitchLink()
    with pytest.raises(HostError) as err:
        _plan(gateway, stub, link, monkeypatch, "addon_disable")
    assert err.value.error["code"] == "addon.already" and err.value.error["message"] == "Terminals is already off."
    link.switch(True)
    with pytest.raises(HostError) as err:
        _plan(gateway, stub, link, monkeypatch, "addon_grant")
    assert err.value.error["code"] == "addon.already" and err.value.error["message"] == "Terminals is already on."
    assert "{" not in err.value.error["message"]
    link.state = "changed"  # updated since the grant: granting again is a real change, not "already"
    assert _plan(gateway, stub, link, monkeypatch, "addon_grant").op == "addon.grant"


def test_an_unreadable_history_prepares_nothing(gateway, stub, monkeypatch):
    class Broken(_SwitchLink):
        def call(self, op, args=None):
            if op == "activity.list":
                raise HostError({"code": "host.unavailable", "message": "down", "retryable": True}, 3)
            return super().call(op, args)

    with pytest.raises(HostError):
        _plan(gateway, stub, Broken(), monkeypatch, "addon_grant")


def test_an_unrelated_event_of_the_same_name_is_not_the_head(gateway, stub, monkeypatch):
    link = _SwitchLink()
    before = _plan(gateway, stub, link, monkeypatch, "addon_grant").idem(stub.ws)
    link.events.append({"id": "9.e", "type": "addon.purged", "data": {"name": "terminals"}})
    assert _plan(gateway, stub, link, monkeypatch, "addon_grant").idem(stub.ws) == before


# ---- R3-A1: versions, read only ---------------------------------------------------------------------------
class _VersionLink:
    def __init__(self, release="0123456789ab", addons=None):
        self.welcome = {"release": release, "ops": {"addon.grant": 1, "addon.disable": 1}}
        self.addons = addons if addons is not None else []

    def supports(self, op, cap):
        return False

    def call(self, op, args=None):
        assert op == "addon.list"  # the only host read the add-ons list makes here
        return {"addons": self.addons}


def _get(client, token, stub, tail):
    return client.get(f"/api/w/{stub.ws}/{tail}", headers={"Authorization": f"Bearer {token}"})


def test_versions_need_a_session_and_report_package_version_and_release(client, token, stub, gateway, monkeypatch):
    assert client.get(f"/api/w/{stub.ws}/versions").status_code == 401
    monkeypatch.setattr(gateway, "enabled", lambda ws: _VersionLink())
    out = _get(client, token, stub, "versions").json()
    assert out["core"]["release"] == "0123456789ab"
    assert out["core"]["version"] and out["dashboard"]["version"]  # both packages are installed in the test env


@pytest.mark.parametrize("release", [None, 7, "short", "0123456789AB", "0123456789ab\n", "<script>x</script>"])
def test_an_odd_release_is_dropped_not_shown(client, token, stub, gateway, monkeypatch, release):
    monkeypatch.setattr(gateway, "enabled", lambda ws: _VersionLink(release=release))
    assert _get(client, token, stub, "versions").json()["core"]["release"] is None


def test_each_addon_row_carries_the_manifest_version_and_odd_ones_are_dropped(
    client, token, stub, gateway, monkeypatch
):
    listed = [
        {"name": "worktrees", "state": "active", "version": "1.2.3"},
        {"name": "publish", "state": "not_granted", "version": "<b>x</b>"},
        {"name": "usage", "state": "active", "version": 7},
    ]
    link = _VersionLink(addons=listed)
    monkeypatch.setattr(gateway, "enabled", lambda ws: link)
    monkeypatch.setattr(gateway, "call", lambda ws, op, args=None: {"runner": None} if op == "whoami" else {})
    rows = {r["id"]: r for r in _get(client, token, stub, "add-ons").json()["add_ons"]}
    assert rows["worktrees"]["version"] == "1.2.3"
    assert rows["publish"]["version"] is None and rows["usage"]["version"] is None
    assert rows["estimate"]["version"] is None  # not listed by the host
    assert rows["dashboard"]["version"] and rows["quick"]["version"]  # the install's own packages


def test_clean_version_accepts_versions_and_nothing_else():
    from orch_dashboard.addon_switches import clean_version

    for good in ("1.2.3", "0.1.0", "1.0.0+abc123", "2.0.0-rc1"):
        assert clean_version(good) == good
    for bad in ("", " 1.0", "1.0\n", "v" * 41, "1.0 <b>", None, 3, "‮1.0", "../x"):
        assert clean_version(bad) is None
