# ruff: noqa: F811
"""Opus review of G6 B1 (#1181): the adversarial probes, now tests. A signed leaf must always be in the plain review and
every leaf's type must be visible."""

from __future__ import annotations

import pytest

from orch.addons import settings_schema
from orch.addons import settings_value as sv
from orch.host import signcontent
from tests.host import human_helpers
from tests.host import market_helpers as M
from tests.host.market_helpers import add_market, err, files
from tests.model.test_hub_replay import hub_actor, hub_fields, w  # noqa: F401

net = M.net
lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


class Ctx:
    @staticmethod
    def note(bound):
        return None if bound else "per host, not signed"


def rows(value, before=None):
    out: dict = {}
    head = signcontent._addons_rows(out, value, before or {}, Ctx)
    return head, out.get("fields_changed", [])


DOT = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "a.b": {"type": "string", "maxLength": 20},
        "a": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"b": {"type": "string", "maxLength": 20}},
        },
    },
}


def test_a_dot_in_a_key_is_refused_by_the_schema_subset_and_the_value_check():
    with pytest.raises(settings_schema.SettingsSchemaError):
        settings_schema.check_settings_schema(DOT)
    assert sv.check_changes({"demo": {"a.b": "shown", "a": {"b": "hidden"}}}) is not None


def test_two_leaves_on_one_path_are_refused_and_never_rendered(monkeypatch):
    """Belt: whatever path syntax comes later, a collision is refused by the value check and gives no plain view."""
    monkeypatch.setattr(sv, "leaves", lambda v, path="": [("a.b", "shown"), ("a.b", "hidden")])
    assert sv.check_shape({"a": 1}) == "two values on one path"
    assert rows.__globals__["signcontent"]._addons_rows({}, {"demo": {"a": 1}}, {}, Ctx) is None


def test_every_signed_leaf_is_in_the_review():
    _, got = rows({"demo": {"a": {"b": "hidden"}, "a-b": "shown"}})
    assert sorted(r["after"] for r in got) == ['"hidden"', '"shown"']


def test_string_and_bool_render_differently():
    for good, bad in ((True, "true"), (5, "5"), (None, "null"), ({}, "{}"), ([], "[]")):
        _, a = rows({"demo": {"x": good}})
        _, b = rows({"demo": {"x": bad}})
        assert a[0]["after"] != b[0]["after"], (good, bad)


def test_a_string_cannot_imitate_the_removed_marker():
    _, got = rows({"demo": {"x": "(removed)"}}, {"demo": {"x": "a"}})
    _, gone = rows({"demo": {}}, {"demo": {"x": "a"}})
    assert got[0]["after"] == '"(removed)"'
    assert got[0]["after"] != [r for r in gone if r["field"] == "demo: x"][0]["after"]


def test_the_note_does_not_claim_privacy():
    out: dict = {}
    signcontent._addons_rows(out, {"demo": {"x": 1}}, {}, Ctx)
    text = " ".join(out["notes"])
    assert "plain text" in text and "Only demo" not in text


def test_hub_cannot_set_addons(w):  # noqa: F811
    _, r = w.try_(
        "workspace", "settings.changed", hub_actor(), roster_v=w.roster_v, set={"addons": {"demo": {}}}, **hub_fields()
    )
    assert r is not None and r.code == "settings.invalid"


def test_the_dot_collision_package_cannot_even_be_installed(served, human, net):
    net.add(
        "dotdemo",
        "1.0.0",
        pkg=files("dotdemo", caps=(), api=">=1.1,<2", settings={"title": "Dot", "schema": DOT}),
        entry_caps=(),
    )
    add_market(served, net)
    assert err(M.stage(served, "dotdemo")) is not None
