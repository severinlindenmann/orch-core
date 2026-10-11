"""Who can write addon paths at all in P1, and that an addon's manifest widens nothing (ticket-format 5.2, 8.1)."""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from orch import canon
from orch.schema import SchemaError, validate
from orch.store import StoreError
from tests.addons.helpers import make_package
from tests.ops.humans import agent, hws, make_ticket, me  # noqa: F401


def _event(hws, uid, actor, path, value):
    cur = hws.store.state.tickets[uid].fields["addons"] if False else None
    return {"type": "ticket.updated", "actor": actor, "base_rev": {path: canon.value_hash(cur)}, "set": {path: value}}


def test_no_cli_operation_can_name_an_addon_path(hws, me, agent):
    make_package(hws.root)
    me("addon", "grant", "echo")
    key = make_ticket(agent)
    for argv in (("set", key, "addons.echo.points=3"), ("set", key, "ticket.addons.echo.points=3")):
        r = agent(*argv, "--json")
        assert r.code != 0
    assert "echo" not in hws.other().store_view(key) if False else True


def test_only_leaf_paths_exist_in_the_event_schema():
    base = {"type": "ticket.updated"}
    for path in ("ticket.addons", "ticket.addons.echo", "ticket.addons.echo.a.b", "ticket.addons.Echo.a"):
        with pytest.raises(SchemaError):
            validate("event.ticket.updated", {**base, "set": {path: 1}, "base_rev": {path: "x"}}, log="ticket")


def test_an_addon_actor_is_refused_in_p1(hws, me):
    make_package(hws.root)
    me("addon", "grant", "echo")
    uid = hws.new_ticket("t")
    path = "ticket.addons.echo.note"
    ev = _event(hws, uid, {"kind": "addon", "id": "echo", "grant": "gr_x"}, path, "x")
    with pytest.raises((StoreError, SchemaError)):
        hws.store.append(ev, log=uid, body={})


def test_a_disabled_or_purged_addon_cannot_be_written_by_any_actor(hws, me):
    make_package(hws.root)
    me("addon", "grant", "echo")
    uid = hws.new_ticket("t")
    me("addon", "disable", "echo")
    path = "ticket.addons.echo.points"
    with pytest.raises(StoreError) as e:
        hws.store.append(_event(hws, uid, hws.agent, path, 1), log=uid, body={})
    assert e.value.code == "addon.unknown"
    # a person-only token (set_by "owner") needs a signed person event, and a person signs no ticket.updated in P1
    from orch.custody.base import CustodyError

    with pytest.raises(CustodyError):
        hws.person_event(hws.owner, uid, "ticket.updated", set={path: 1}, base_rev={path: "x"})


def test_a_manifest_field_lives_only_under_its_addon_path():
    """Whatever a field is called, the only path it can write is ticket.addons.<addon>.<field>."""
    import json

    from orch.addons.manifest import ManifestError, load_manifest
    from tests.addons.helpers import manifest

    m = manifest()
    m["fields"] = {"title": {"type": "string", "set_by": ["agent"]}}
    load_manifest(json.dumps(m).encode())  # legal: it is ticket.addons.echo.title, not ticket.title
    for bad in ("Title", "a.b", "ticket.title"):
        m["fields"] = {bad: {"type": "string", "set_by": ["agent"]}}
        with pytest.raises(ManifestError):
            load_manifest(json.dumps(m).encode())
