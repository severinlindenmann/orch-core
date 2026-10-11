"""Replay enforces the declarations of ``addon.granted`` (ticket-format §5.4.2, §8.1): per actor kind, with persons
holding tokens, sections and artifact kinds, re-grant and purge."""

from __future__ import annotations

import pytest

from orch import canon
from tests.model.world import World, digest, refused

FIELDS = {
    "points": {"type": "integer", "min": 0, "max": 100, "set_by": ["owner", "agent", "member"], "gate": ["plan"]},
    "mood": {"type": "enum", "values": ["good", "bad"], "set_by": ["owner"]},
    "boss": {"type": "string", "set_by": ["ticket_owner"]},
    "auto": {"type": "string", "set_by": ["addon"]},
}
SECTIONS = [{"id": "echo.notes", "types": ["feature"], "gate": ["plan"]}, {"id": "echo.free", "types": ["bug"]}]


def grant(w, fields=None, sections=None, kinds=("chart",), pkg="pkg"):
    return w.wev(
        "addon.granted",
        "sev",
        name="echo",
        version="1.0.0",
        package_sha256=digest(pkg),
        capabilities=[],
        fields=FIELDS if fields is None else fields,
        sections=SECTIONS if sections is None else sections,
        artifact_kinds=list(kinds),
    )


@pytest.fixture
def env():
    w = World().bootstrap({"mara": "maintainer", "vik": "member", "vera": "viewer"})
    uid = w.ticket()
    grant(w)
    return w, uid


def set_(w, uid, actor, field, value):
    path = f"ticket.addons.echo.{field}"
    cur = (w.view(uid).fields["addons"].get("echo") or {}).get(field)
    return refused(w, uid, "ticket.updated", actor, base_rev={path: canon.value_hash(cur)}, set={path: value})


def test_a_person_needs_a_token_the_field_names(env):
    w, uid = env
    assert set_(w, uid, "sev", "mood", "good") is None  # owner
    assert set_(w, uid, "mara", "mood", "good") == "role.denied"  # a maintainer is not an owner
    assert set_(w, uid, "vik", "points", 5) is None  # "member" is named
    assert set_(w, uid, "mara", "points", 5) == "role.denied"  # a maintainer holds "maintainer", not "member"
    assert set_(w, uid, "vera", "points", 5) == "role.denied"  # a viewer holds no token
    assert set_(w, uid, "sev", "boss", "x") is None  # the ticket owner is sev
    assert set_(w, uid, "mara", "boss", "x") == "role.denied"


def test_a_person_cannot_pass_as_agent_or_addon(env):
    w, uid = env
    assert set_(w, uid, "sev", "auto", "x") == "role.denied"  # "addon" is no token a person holds
    assert set_(w, uid, "vik", "auto", "x") == "role.denied"


def test_agents_need_a_grant_and_agent_in_set_by(env):
    w, uid = env
    gid = w.grant("sev")
    agent = w.agent("sev", gid)
    assert set_(w, uid, agent, "points", 5) is None
    assert set_(w, uid, agent, "mood", "good") == "role.denied"
    assert set_(w, uid, agent, "auto", "x") == "role.denied"
    assert set_(w, uid, agent, "points", 1_000_000) == "addon.value_invalid"
    assert set_(w, uid, agent, "ghost", 1) == "addon.field_unknown"
    assert set_(w, uid, w.unattended(), "points", 5) is not None


def test_the_values_of_every_type_are_checked(env):
    w, uid = env
    assert set_(w, uid, "sev", "mood", "evil") == "addon.value_invalid"
    assert set_(w, uid, "sev", "mood", None) is None
    assert set_(w, uid, "vik", "points", -1) == "addon.value_invalid"
    assert set_(w, uid, "vik", "points", 100) is None


def test_sections_must_be_declared_for_the_ticket_type(env):
    w, uid = env
    ok = lambda sid, text="x": refused(  # noqa: E731
        w,
        uid,
        "ticket.updated",
        "sev",
        base_rev={"body." + sid: canon.section_hash("")},
        sections={sid: {"hash": canon.section_hash(text), "refs": []}},
    )
    assert ok("echo.notes") is None  # declared for feature
    assert ok("echo.free") == "body.unknown_section"  # declared for bug only
    assert ok("echo.nope") == "body.unknown_section"
    assert ok("ghost.notes") == "addon.unknown"


def artifact(w, uid, kind, addon="echo", name="c.png"):
    gid = w.grant("sev")
    return refused(w, uid, "artifact.added", w.agent("sev", gid), name=name, kind=kind, addon=addon, ref="r")


def test_addon_artifact_kinds_must_be_declared(env):
    w, uid = env
    assert artifact(w, uid, "chart") is None
    assert artifact(w, uid, "map") == "artifact.kind"
    assert artifact(w, uid, "chart", addon="ghost") == "addon.unknown"


def test_a_disabled_addon_has_no_live_fields_sections_or_kinds(env):
    w, uid = env
    w.wev("addon.disabled", "sev", name="echo")
    assert set_(w, uid, "sev", "mood", "good") == "addon.unknown"
    assert artifact(w, uid, "chart") == "addon.unknown"


def test_purge_removes_fields_sections_and_artifacts_and_a_new_grant_starts_empty(env):
    w, uid = env
    gid = w.grant("sev")
    w.edit(uid, "sev", sets={"ticket.addons.echo.mood": "good"}, sections={"echo.notes": "text"})
    w.tev(uid, "artifact.added", w.agent("sev", gid), name="c.png", kind="chart", addon="echo", ref="r")
    v = w.view(uid)
    assert v.fields["addons"]["echo"]["mood"] == "good" and "echo.notes" in v.sections and v.artifacts
    w.wev("addon.disabled", "sev", name="echo")
    v = w.view(uid)  # a disable keeps everything
    assert v.fields["addons"]["echo"]["mood"] == "good" and "echo.notes" in v.sections and v.artifacts
    w.wev("addon.purged", "sev", name="echo")
    v = w.view(uid)
    assert "echo" not in v.fields["addons"] and "echo.notes" not in v.sections and not v.artifacts
    grant(w)
    v = w.view(uid)
    assert "echo" not in v.fields["addons"] and not v.artifacts


def test_a_regrant_drops_stored_values_the_new_grant_does_not_allow(env):
    w, uid = env
    w.edit(
        uid,
        "sev",
        sets={"ticket.addons.echo.mood": "good", "ticket.addons.echo.points": 50},
        sections={"echo.notes": "text"},
    )
    w.wev("addon.disabled", "sev", name="echo")
    # a new version: mood is now an integer, points has a lower maximum, notes is for bugs only
    new = {
        "mood": {"type": "integer", "set_by": ["owner"]},
        "points": {"type": "integer", "min": 0, "max": 10, "set_by": ["owner"]},
    }
    grant(w, fields=new, sections=[{"id": "echo.notes", "types": ["bug"]}], pkg="pkg2")
    v = w.view(uid)
    assert "echo" not in v.fields["addons"] and "echo.notes" not in v.sections


def test_a_regrant_keeps_values_that_are_still_valid(env):
    w, uid = env
    w.edit(uid, "sev", sets={"ticket.addons.echo.points": 5, "ticket.addons.echo.mood": "good"})
    grant(w, fields={**FIELDS, "mood": {"type": "integer", "set_by": ["owner"]}}, pkg="pkg2")
    assert dict(w.view(uid).fields["addons"]["echo"]) == {"points": 5}  # mood no longer fits an integer


def test_the_grant_not_a_later_manifest_is_the_authority(env):
    """Replay has no manifest: a field set_by widened in a never-granted manifest changes nothing until a new grant."""
    w, uid = env
    assert set_(w, uid, "mara", "mood", "good") == "role.denied"
    grant(w, fields={**FIELDS, "mood": {**FIELDS["mood"], "set_by": ["owner", "maintainer"]}}, pkg="pkg2")
    assert set_(w, uid, "mara", "mood", "good") is None
