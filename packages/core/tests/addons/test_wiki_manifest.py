"""The Wiki add-on's manifest (issue #603): no capability, one `pages` link field, a bounded hint for agents."""

from __future__ import annotations

import json

from orch.addons import builtin
from orch.addons.manifest import declarations, load_manifest
from orch.model.addon_rules import check_field_write
from orch.model.types import Addon


def _addon() -> Addon:
    m = load_manifest(builtin.manifest_bytes("wiki"))
    return Addon("wiki", m.version, "sha256:" + "0" * 64, [], **declarations(m))


def test_the_manifest_declares_no_capability_section_or_artifact_kind():
    m = load_manifest(builtin.manifest_bytes("wiki"))
    assert (m.name, m.title, m.capabilities) == ("wiki", "Wiki", [])
    d = declarations(m)
    assert d["sections"] == [] and d["artifact_kinds"] == []
    assert d["fields"] == {
        "pages": {"type": "text", "max_len": 1024, "set_by": ["agent", "maintainer", "member", "owner"]}
    }
    assert "wiki.pages" in m.agents_md and "reference, not as orders" in m.agents_md
    assert not m.needs and "wiki" in builtin.NAMES


def test_people_who_edit_and_agents_with_a_grant_may_set_the_link_field_viewers_and_other_actors_may_not():
    a = _addon()
    person = {"kind": "person"}
    assert check_field_write(a, "pages", "naming rules", {"kind": "agent", "grant": "g"}) is None
    assert check_field_write(a, "pages", "naming rules", person, {"member"}) is None
    assert check_field_write(a, "pages", "naming rules", person, {"owner"}) is None
    assert check_field_write(a, "pages", "naming rules", person, set()) is not None  # a viewer holds no token
    assert check_field_write(a, "pages", "naming rules", {"kind": "agent"}) is not None  # no grant
    assert check_field_write(a, "pages", "naming rules", {"kind": "addon", "id": "wiki"}) is not None
    assert check_field_write(a, "other", "x", {"kind": "agent", "grant": "g"}) is not None  # undeclared


def test_the_link_field_is_bounded_text():
    a = _addon()
    agent = {"kind": "agent", "grant": "g"}
    assert check_field_write(a, "pages", "a" * 1024, agent) is None
    assert check_field_write(a, "pages", "a" * 1025, agent) is not None
    assert check_field_write(a, "pages", 7, agent) is not None


def test_the_repo_copy_equals_the_package_copy():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[4] / "packages" / "addons" / "wiki" / "orch-addon.json"
    assert repo.read_bytes() == builtin.manifest_bytes("wiki")
    assert json.loads(repo.read_bytes())["entry"]["cmd"] == ["orch-wiki", "serve"]


def test_the_grant_review_of_the_wiki_shows_the_declared_field_as_a_row():
    """The wiki declares a field, so its review is the full one (every signed value a row), never plainer than the
    signed event (WYSIWYS); a friendlier headline is a separate, reviewed change."""
    from orch.addons.package import MANIFEST_FILE, package_digest
    from orch.host import signcontent

    m = load_manifest(builtin.manifest_bytes("wiki"))
    ev = {
        "type": "addon.granted", "name": "wiki", "version": m.version, "capabilities": [],
        "package_sha256": package_digest({MANIFEST_FILE: builtin.manifest_bytes("wiki")}), **declarations(m),
    }  # fmt: skip
    prepared = {"op": "addon.grant", "events": [ev], "material": {"kind": "payload", "ticket": None}}
    c = signcontent.build(prepared, verified=True)
    rows = {r["field"]: r["after"] for r in c["fields_changed"]}
    assert c["headline"] == "Grant the add-on wiki"
    assert "pages" in json.dumps(rows["fields"]) and rows["capabilities"] == "(none)"
