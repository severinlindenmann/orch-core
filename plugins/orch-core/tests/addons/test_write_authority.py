"""Who can write addon paths, and that replay enforces the grant (ticket-format §5.4.2, §8.1).

Replay (``Store.append``, the same code a reader runs) checks every addon write against the declarations the owner
signed in ``addon.granted``: declared field, ``set_by`` for the actor, type and limits, declared section and ticket
type, declared artifact kind. Nothing a manifest says after the grant, and nothing a modified host signs, widens that.
"""

# ruff: noqa: F811
from __future__ import annotations

import pytest

from orch import canon
from orch.schema import SchemaError, validate
from orch.store import StoreError
from tests.addons.helpers import make_package
from tests.ops.humans import agent, hws, make_ticket, me  # noqa: F401

PERSON = "p_" + "a" * 32


def granted(hws, me):
    make_package(hws.root)
    assert me("addon", "grant", "echo").code == 0
    return hws.new_ticket("t")


def write(hws, uid, field, value, actor=None):
    path = f"ticket.addons.echo.{field}"
    store = hws.store
    store.refresh()
    cur = store.ticket(uid).fields["addons"].get("echo", {}).get(field)
    return store.append(
        {
            "type": "ticket.updated",
            "actor": actor or hws.agent,
            "base_rev": {path: canon.value_hash(cur)},
            "set": {path: value},
        },
        log=uid,
        body={},
    )


def code_of(hws, uid, field, value, actor=None):
    with pytest.raises(StoreError) as e:
        write(hws, uid, field, value, actor)
    return e.value.code


def test_the_review_probes_are_refused_at_replay(hws, me):
    """The four writes an agent could append to a ticket before the grant carried its declarations."""
    uid = granted(hws, me)
    assert code_of(hws, uid, "mood", "evil") == "role.denied"  # owner-only, gate-bound enum
    assert code_of(hws, uid, "mood", "good") == "role.denied"  # not even a valid value helps
    assert code_of(hws, uid, "reviewer", "p_x") == "role.denied"  # owner-only person field
    assert code_of(hws, uid, "undeclared", [1, {"a": "b"}]) == "addon.field_unknown"
    assert code_of(hws, uid, "points", 1_000_000) == "addon.value_invalid"  # agent may set points, max is 100
    assert code_of(hws, uid, "points", -1) == "addon.value_invalid"
    assert code_of(hws, uid, "points", True) == "addon.value_invalid"
    assert code_of(hws, uid, "points", "5") == "addon.value_invalid"
    assert code_of(hws, uid, "note", "x") == "role.denied"  # set_by is addon only
    assert code_of(hws, uid, "done", "yes") == "addon.value_invalid"
    assert code_of(hws, uid, "tags", ["a"] * 4) == "addon.value_invalid"
    assert "echo" not in hws.store.ticket(uid).fields["addons"]  # nothing of it was applied


def test_what_the_grant_allows_is_accepted_and_null_clears(hws, me):
    uid = granted(hws, me)
    write(hws, uid, "points", 5)
    write(hws, uid, "tags", ["a", "b"])
    write(hws, uid, "done", True)
    assert dict(hws.store.ticket(uid).fields["addons"]["echo"]) == {"points": 5, "tags": ("a", "b"), "done": True}
    write(hws, uid, "points", None)
    assert hws.store.ticket(uid).fields["addons"]["echo"]["points"] is None  # cleared: null, as the gate hash reads it


def test_an_unattended_agent_is_not_an_agent_with_a_grant(hws, me):
    uid = granted(hws, me)
    unattended = {k: v for k, v in hws.agent.items() if k not in ("grant", "for")} | {"unattended": True}
    with pytest.raises(StoreError):
        write(hws, uid, "points", 5, unattended)


def test_an_addon_actor_is_refused_in_p1(hws, me):
    uid = granted(hws, me)
    with pytest.raises((StoreError, SchemaError)):
        write(hws, uid, "note", "x", {"kind": "addon", "id": "echo", "grant": "gr_x"})


def test_a_disabled_or_purged_addon_cannot_be_written_by_any_actor(hws, me):
    uid = granted(hws, me)
    me("addon", "disable", "echo")
    assert code_of(hws, uid, "points", 1) == "addon.unknown"
    me("addon", "purge", "echo")
    assert code_of(hws, uid, "points", 1) == "addon.unknown"


def test_a_person_signs_no_ticket_update_in_p1(hws, me):
    """So a human-token ``set_by`` (owner, maintainer...) has no route in P1: the custody layer refuses to prompt."""
    from orch.custody.base import CustodyError

    uid = granted(hws, me)
    path = "ticket.addons.echo.mood"
    with pytest.raises(CustodyError):
        hws.person_event(hws.owner, uid, "ticket.updated", set={path: "good"}, base_rev={path: "x"})


def artifact(hws, uid, kind, name="c.png", addon="echo"):
    return hws.store.append(
        {
            "type": "artifact.added",
            "actor": hws.agent,
            "name": name,
            "kind": kind,
            "addon": addon,
            "ref": "https://x/y",
        },
        log=uid,
    )


def test_addon_artifact_kinds_must_be_declared(hws, me):
    uid = granted(hws, me)
    artifact(hws, uid, "chart")
    for kind, addon, code in (("map", "echo", "artifact.kind"), ("chart", "ghost", "addon.unknown")):
        with pytest.raises(StoreError) as e:
            artifact(hws, uid, kind, f"{kind}-{addon}.png", addon)
        assert e.value.code == code


def test_undeclared_sections_are_refused(hws, me):
    uid = granted(hws, me)
    store = hws.store
    store.refresh()
    for sid in ("echo.zzz", "ghost.notes"):
        ev = {
            "type": "ticket.updated",
            "actor": hws.agent,
            "base_rev": {f"body.{sid}": canon.section_hash("")},
            "sections": {sid: {"hash": canon.section_hash("x"), "refs": []}},
        }
        with pytest.raises(StoreError) as e:
            store.append(ev, log=uid, body={sid: "x"})
        # the store renders the body before the model judges the event, and P1 has no addon headings to render with;
        # the model's own refusals are pinned in tests/model/test_addon_rules.py
        assert e.value.code in ("body.unknown_section", "addon.unknown", "validation.body"), sid


def test_no_cli_operation_can_name_an_addon_path(hws, me, agent):
    make_package(hws.root)
    me("addon", "grant", "echo")
    key = make_ticket(agent)
    for argv in (("set", key, "addons.echo.points=3"), ("set", key, "ticket.addons.echo.points=3")):
        r = agent(*argv, "--json")
        assert r.code != 0, argv


def test_only_leaf_paths_exist_in_the_event_schema():
    base = {"type": "ticket.updated"}
    for path in ("ticket.addons", "ticket.addons.echo", "ticket.addons.echo.a.b", "ticket.addons.Echo.a"):
        with pytest.raises(SchemaError):
            validate("event.ticket.updated", {**base, "set": {path: 1}, "base_rev": {path: "x"}}, log="ticket")


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
