"""Registration from granted manifests, ``set_by`` per actor, leaf paths, proposals (ticket-format §8, §8.1)."""

from __future__ import annotations

import json

import pytest

from orch.addons.manifest import declarations, load_manifest
from orch.addons.registry import ACTIVE, ProposalError, Registry, WriteRefused, state_of, validate_value
from orch.model.types import Addon
from tests.addons.helpers import MANIFEST, manifest

PERSON = "p_" + "a" * 32
DIGEST = "sha256:" + "1" * 64


def load(**over):
    return load_manifest(json.dumps(manifest(**over)).encode())


def registry(m=None, **flags):
    m = m or load()
    a = Addon(m.name, m.version, DIGEST, m.capabilities, **declarations(m))
    digest = flags.pop("digest", DIGEST)
    for k, v in flags.items():
        setattr(a, k, v)
    return Registry({m.name: a}, {m.name: (m, digest)})


def refusal(reg, *a, **kw):
    with pytest.raises(WriteRefused) as e:
        reg.check_write(*a, **kw)
    return e.value.code


def test_a_granted_enabled_manifest_registers_fields_sections_and_kinds():
    r = registry()
    assert r.state("echo") == ACTIVE and r.active() == ["echo"]
    assert r.field_spec("echo", "points")["type"] == "integer"
    assert r.field_spec("echo", "nope") is None and r.field_spec("other", "points") is None
    assert r.section_heading("echo.notes") == "Echo notes" and r.section_heading("echo.zzz") is None
    assert r.sections_for("feature") == [("echo.notes", "Echo notes", "plan"), ("echo.extra", "Echo extra", "context")]
    assert r.sections_for("bug") == [("echo.notes", "Echo notes", "plan")]
    assert r.sections_for("epic") == []
    assert r.artifact_kinds("echo") == ["chart"] and r.artifact_kinds("other") == []


def test_set_by_is_checked_per_actor():
    r = registry()
    agent = {"kind": "agent", "id": "claude-code", "grant": "gr_x"}
    unattended = {"kind": "agent", "id": "claude-code"}
    person = {"kind": "person", "id": PERSON}
    addon = {"kind": "addon", "id": "echo"}
    # points: owner, maintainer, agent, addon
    r.check_write("echo", "points", 3, agent)
    r.check_write("echo", "points", 3, addon)
    r.check_write("echo", "points", 3, person, tokens={"owner"})
    r.check_write("echo", "points", 3, person, tokens={"member", "maintainer"})  # one matching token is enough
    assert refusal(r, "echo", "points", 3, unattended) == "role.denied"  # an agent without a grant
    assert refusal(r, "echo", "points", 3, person, tokens={"member"}) == "role.denied"
    assert refusal(r, "echo", "points", 3, person, tokens=()) == "role.denied"
    assert refusal(r, "echo", "points", 3, {"kind": "host"}) == "role.denied"
    assert refusal(r, "echo", "points", 3, {"kind": "addon", "id": "other"}) == "role.denied"  # only that addon
    # note: addon only
    r.check_write("echo", "note", "hi", addon)
    assert refusal(r, "echo", "note", "hi", agent) == "role.denied"
    assert refusal(r, "echo", "note", "hi", person, tokens={"owner"}) == "role.denied"
    # a person can never be "agent" or "addon" by naming it as a token
    assert refusal(r, "echo", "note", "hi", person, tokens={"addon", "agent"}) == "role.denied"
    # done: agent only
    r.check_write("echo", "done", True, agent)
    assert refusal(r, "echo", "done", True, person, tokens={"owner"}) == "role.denied"


def test_unknown_addon_or_field():
    r = registry()
    assert refusal(r, "ghost", "x", 1, {"kind": "addon", "id": "ghost"}) == "addon.unknown"
    assert refusal(r, "echo", "ghost", 1, {"kind": "addon", "id": "echo"}) == "addon.field_unknown"


@pytest.mark.parametrize(
    "field,good,bad",
    [
        ("points", [0, 100, None], [101, -1, True, 1.5, "5", [5], 2**60]),
        ("note", ["x" * 50, "a b"], ["", "x" * 51, "a\nb", 3, "a\x1bb"]),
        ("memo", ["a\nb"], ["x" * 101, "", "a\x00"]),
        ("mood", ["good", None], ["meh", "GOOD", 1]),
        ("tags", [[], ["a", "b", "c"]], [["a"] * 4, ["a", "a"], ["a\nb"], "a", [1], [""]]),
        ("reviewer", [PERSON], ["p_x", "sev", PERSON + "0", 5]),
        ("done", [True, False], [1, 0, "true"]),
    ],
)
def test_values_are_checked_against_the_field_type(field, good, bad):
    spec = MANIFEST["fields"][field]
    for v in good:
        validate_value(spec, v)
    for v in bad:
        with pytest.raises(ValueError):
            validate_value(spec, v)


def test_check_write_refuses_a_bad_value():
    r = registry()
    assert refusal(r, "echo", "points", 1000, {"kind": "addon", "id": "echo"}) == "addon.value_invalid"


def test_section_writes_need_the_section_to_exist_for_the_ticket_type():
    r = registry()
    r.check_section_write("echo.notes", "feature")
    r.check_section_write("echo.notes", "bug")
    for sid, ty in (("echo.extra", "bug"), ("echo.zzz", "feature"), ("echo.notes", "epic")):
        with pytest.raises(WriteRefused) as e:
            r.check_section_write(sid, ty)
        assert e.value.code == "body.unknown_section"
    with pytest.raises(WriteRefused) as e:
        r.check_section_write("ghost.notes", "feature")
    assert e.value.code == "addon.unknown"


# -------------------------------------------------------------------------------------------------- proposals


def test_a_proposal_is_translated_to_leaf_paths_and_section_ids():
    r = registry()
    p = r.check_proposal(
        "echo",
        "feature",
        {
            "set": {"points": 5, "note": "hello"},
            "sections": {"notes": "text"},
            "artifacts": [{"kind": "chart", "name": "c.png", "ref": "https://x/y"}],
        },
    )
    assert p.set == {"ticket.addons.echo.points": 5, "ticket.addons.echo.note": "hello"}
    assert p.sections == {"echo.notes": "text"}
    assert p.artifacts == [{"kind": "chart", "name": "c.png", "ref": "https://x/y"}]
    assert r.check_proposal("echo", "feature", {}).set == {}


@pytest.mark.parametrize(
    "result",
    [
        [],
        "x",
        {"events": []},  # no core event, ever
        {"set": {"ticket.title": "x"}},  # a core path
        {"set": {"ticket.addons.echo.points": 1}},  # a full path instead of a field name
        {"set": {"addons.other.points": 1}},
        {"set": {"mood": "good"}},  # set_by has no addon
        {"set": {"done": True}},
        {"set": {"nope": 1}},
        {"set": {"points": 1000}},
        {"set": []},
        {"sections": {"extra": "x"}},  # not a section for the ticket type of this call (bug)
        {"sections": {"echo.notes": "x"}},  # a qualified id
        {"sections": {"notes": 5}},
        {"sections": {"notes": "x\n"}},
        {"sections": {"notes": "## Plan\nforged"}},
        {"sections": {"notes": "```\nopen fence"}},
        {"sections": {"notes": "x\x00"}},
        {"sections": {"notes": "x" * 70_000}},
        {"artifacts": [{"kind": "screenshot", "name": "a.png", "ref": "r"}]},  # a core kind
        {"artifacts": [{"kind": "chart", "name": "../a.png", "ref": "r"}]},
        {"artifacts": [{"kind": "chart", "name": "a.png", "ref": "r", "sha256": "x"}]},
        {"artifacts": [{"kind": "chart", "name": "a.png", "ref": "a\nb"}]},
        {"artifacts": [{"kind": "chart", "name": "a.png", "ref": ""}]},
        {"artifacts": [{"kind": "chart", "name": "a.png"}]},
        {"artifacts": ["x"]},
        {"artifacts": [{"kind": "chart", "name": f"a{i}.png", "ref": "r"} for i in range(33)]},
    ],
)
def test_proposals_that_name_anything_else_are_refused_whole(result):
    with pytest.raises(ProposalError):
        registry().check_proposal("echo", "bug", result)


def test_an_inactive_or_unknown_addon_proposes_nothing():
    for reg in (registry(enabled=False), registry(purged=True), Registry({}, {})):
        with pytest.raises(ProposalError):
            reg.check_proposal("echo", "feature", {})


def test_the_proposal_never_carries_more_than_its_addon():
    p = registry().check_proposal("echo", "feature", {"set": {"points": 1}, "sections": {"extra": "x"}})
    assert all(k.startswith("ticket.addons.echo.") for k in p.set) and all(k.startswith("echo.") for k in p.sections)


def test_state_of_covers_every_case():
    m = load()
    a = Addon("echo", "1.0.0", DIGEST, [], {}, [], [])
    assert state_of(None, None) == "unknown"
    assert state_of(a, (m, DIGEST)) == "active"
    assert state_of(a, None) == "missing"
    assert state_of(a, (m, "sha256:" + "2" * 64)) == "changed"
    assert state_of(a, (load(version="1.0.1"), DIGEST)) == "changed"
    a.enabled = False
    assert state_of(a, (m, DIGEST)) == "disabled"
    a.purged = True
    assert state_of(a, (m, DIGEST)) == "purged"


def test_headings_taken_by_another_active_addon_are_reported():
    echo = load()
    other = load(
        name="other", title="Other", sections=[{"id": "n", "heading": "Other notes", "after": "plan", "types": ["bug"]}]
    )
    reg = Registry(
        {"echo": Addon("echo", "1.0.0", DIGEST, [], **declarations(echo))},
        {"echo": (echo, DIGEST)},
    )
    assert reg.heading_conflicts(other) == []
    clash = json.loads(json.dumps(manifest(name="other", title="Other")))
    clash["sections"] = [{"id": "n", "heading": "ECHO-notes", "after": "plan", "types": ["bug"]}]
    assert reg.heading_conflicts(load_manifest(json.dumps(clash).encode())) == ["ECHO-notes"]
    mine = load()
    assert reg.heading_conflicts(mine) == []  # an addon does not collide with itself (a re-grant)
