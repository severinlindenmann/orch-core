"""Data of a disabled or purged addon: shown as inactive, never counted, never writable (ticket-format §8.1)."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from orch import canon
from orch.addons import install
from orch.addons.needs import needs_of_ticket
from orch.addons.registry import view_data
from orch.store import StoreError
from tests.addons.helpers import make_package, manifest
from tests.ops.humans import hws, me  # noqa: F401

POINTS = "ticket.addons.echo.points"


def write(hws, uid, value, field="points"):
    s = hws.store
    s.refresh()
    cur = s.state.tickets[uid].fields["addons"].get("echo", {}).get(field)
    path = f"ticket.addons.echo.{field}"
    return s.append(
        {
            "type": "ticket.updated",
            "actor": hws.agent,
            "base_rev": {path: canon.value_hash(cur)},
            "set": {path: value},
        },
        log=uid,
        body={},
    )


class Fresh:
    """The store after a refresh: ``ticket`` (views) and ``workspace`` (the view)."""

    def __init__(self, store):
        store.refresh()
        self.ticket, self.workspace = store.ticket, store.state.workspace


def refresh(hws):
    return Fresh(hws.store)


def setup(hws, me, man=None):
    make_package(hws.root, man=man)
    assert me("addon", "grant", "echo").code == 0
    uid = hws.new_ticket("with an addon field")
    write(hws, uid, 5)
    return uid


def plan_hash(hws, uid):
    return refresh(hws).ticket(uid).gates["plan"].hash


def test_live_data_is_bound_by_its_gate_and_shown_active(hws, me):
    uid = setup(hws, me)
    state = refresh(hws)
    assert state.ticket(uid).fields["addons"]["echo"]["points"] == 5
    reg = install.load_registry(hws.root, state.workspace.addons)
    assert view_data(reg, state.ticket(uid).fields["addons"]) == {
        "echo": {"state": "active", "active": True, "fields": {"points": 5}}
    }
    h = plan_hash(hws, uid)
    write(hws, uid, 6)
    assert plan_hash(hws, uid) != h  # a bound field is part of the plan gate


def test_disabled_data_stays_but_is_inactive_and_not_bound(hws, me):
    uid = setup(hws, me)
    live = plan_hash(hws, uid)
    assert me("addon", "disable", "echo").code == 0
    state = refresh(hws)
    assert state.ticket(uid).fields["addons"]["echo"]["points"] == 5  # untouched
    reg = install.load_registry(hws.root, state.workspace.addons)
    assert view_data(reg, state.ticket(uid).fields["addons"])["echo"] == {
        "state": "disabled",
        "active": False,
        "fields": {"points": 5},
    }
    assert plan_hash(hws, uid) != live  # no longer part of the hash input: an inactive field is not bound
    assert reg.active() == [] and reg.field_spec("echo", "points") is None and reg.sections_for("feature") == []


def test_writes_to_a_disabled_or_purged_addon_are_refused(hws, me):
    uid = setup(hws, me)
    me("addon", "disable", "echo")
    with pytest.raises(StoreError) as e:
        write(hws, uid, 7)
    assert e.value.code == "addon.unknown"
    assert refresh(hws).ticket(uid).fields["addons"]["echo"]["points"] == 5
    me("addon", "purge", "echo")
    with pytest.raises(StoreError) as e:
        write(hws, uid, 7)
    assert e.value.code == "addon.unknown"


def test_purge_removes_the_data_from_the_derived_state_of_every_ticket(hws, me):
    uid = setup(hws, me)
    uid2 = hws.new_ticket("another")
    write(hws, uid2, 9)
    live = plan_hash(hws, uid)
    assert me("addon", "purge", "echo").code == 0
    state = refresh(hws)
    for u in (uid, uid2):
        assert "echo" not in state.ticket(u).fields["addons"]
    reg = install.load_registry(hws.root, state.workspace.addons)
    assert reg.state("echo") == "purged"
    assert plan_hash(hws, uid) != live
    # the data of a purged addon is not in the ticket.json on disk either (it is rebuilt from the same state)
    assert b'"echo"' not in (hws.path(uid, "ticket.json")).read_bytes().replace(b'"name": "echo"', b"")


def test_a_new_grant_after_purge_starts_empty(hws, me):
    uid = setup(hws, me)
    me("addon", "purge", "echo")
    assert me("addon", "grant", "echo").code == 0
    assert "echo" not in refresh(hws).ticket(uid).fields["addons"]
    write(hws, uid, 1)
    assert refresh(hws).ticket(uid).fields["addons"]["echo"] == {"points": 1}


def test_data_of_a_never_granted_addon_is_shown_unknown_and_never_active(hws, me):
    reg = install.load_registry(hws.root, {})
    shown = view_data(reg, {"ghost": {"x": 1}})
    assert shown == {"ghost": {"state": "unknown", "active": False, "fields": {"x": 1}}}


def test_inactive_data_is_never_an_input_of_a_needs_rule(hws, me):
    rule = {"id": "big", "when": ["gt", ["field", "points"], 3], "who": ["owner"], "text": "large estimate"}
    uid = setup(hws, me, man=manifest(needs=[rule]))
    state = refresh(hws)
    reg = install.load_registry(hws.root, state.workspace.addons)
    got = needs_of_ticket(reg, state.workspace, state.ticket(uid))
    assert [(n.kind, n.ref, n.detail) for n in got] == [("addon", "echo.big", "large estimate")]
    assert got[0].who == (hws.owner.ref,)
    me("addon", "disable", "echo")
    state = refresh(hws)
    reg = install.load_registry(hws.root, state.workspace.addons)
    assert (
        needs_of_ticket(reg, state.workspace, state.ticket(uid)) == []
    )  # the data is there, the rule does not read it


def test_a_rule_reaches_the_persons_who_hold_its_tokens_and_can_see_the_ticket(hws, me):
    rule = {
        "id": "look",
        "when": ["eq", ["var", "status"], "open"],
        "who": ["owner", "maintainer", "member"],
        "text": "look",
    }
    maria = hws.add_member("maria", "maintainer")
    hws.add_member("vera", "viewer")  # a viewer holds no token
    uid = setup(hws, me, man=manifest(needs=[rule]))
    state = refresh(hws)
    reg = install.load_registry(hws.root, state.workspace.addons)
    (need,) = needs_of_ticket(reg, state.workspace, state.ticket(uid))
    assert set(need.who) == {hws.owner.ref, maria.ref}
    hws.store.append(
        hws.person_event(hws.owner, uid, "visibility.changed", visibility={"restricted": [hws.owner.ref]}), log=uid
    )
    state = refresh(hws)
    (need,) = needs_of_ticket(reg, state.workspace, state.ticket(uid))
    assert need.who == (hws.owner.ref,)  # the others cannot see the ticket, so they are not told about it
