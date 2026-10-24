"""V3 G3: ``install.diff``: what an update changes and when it ``widened`` what the add-on may do (spec 6, design 3)."""

from __future__ import annotations

import json

import pytest

from orch.addons.manifest import load_manifest
from orch.market import install as mi

BASE = {
    "schema": "orch.addon/3", "name": "x", "version": "1.0.0", "title": "X", "min_core": "2.0.0", "api": ">=1.0,<2",
    "entry": {"cmd": ["python3", "{pkg}/main.py"]}, "capabilities": ["network"],
    "fields": {"mood": {"type": "enum", "values": ["good", "bad"], "set_by": ["owner"]}},
    "sections": [{"id": "notes", "heading": "Notes", "after": "plan", "types": ["feature"]}],
    "artifact_kinds": [{"kind": "chart", "label": "Chart"}],
}  # fmt: skip
NEEDS = {"id": "n", "who": ["owner"], "text": "do it", "when": ["eq", ["var", "status"], "testing"]}
PREV = {"version": "1.0.0", "package_sha256": "sha256:" + "a" * 64, "commit": "c" * 40, "publisher_key": "K"}


def m(**more):
    doc = {**BASE, **more}
    return load_manifest(json.dumps(doc).encode())


def facts(tofu="known"):
    return mi.Facts({}, tofu, None if tofu == "unsigned" else "K", PREV)


def d(old, new, tofu="known", prev=PREV):
    return mi.diff(old, new, prev, facts(tofu))


def test_nothing_but_the_version_is_not_widened():
    got = d(m(), m(version="1.0.1"))
    assert got["widened"] is False and got["changes"] == ["version 1.0.0 -> 1.0.1"] and got["publisher"] == "same"
    assert got["from"] == {"version": "1.0.0", "package_sha256": PREV["package_sha256"], "commit": "c" * 40}


@pytest.mark.parametrize(
    ("change", "key"),
    [
        ({"capabilities": ["network", "pty"]}, "capabilities"),
        ({"fields": {**BASE["fields"], "extra": {"type": "boolean", "set_by": ["owner"]}}}, "fields"),
        (
            {
                "sections": [
                    *BASE["sections"],
                    {"id": "more", "types": ["feature"], "heading": "More", "after": "context"},
                ]
            },
            "sections",
        ),
        ({"artifact_kinds": [*BASE["artifact_kinds"], {"kind": "graph", "label": "Graph"}]}, "artifact_kinds"),
        ({"needs": [NEEDS]}, "needs"),
    ],
)
def test_more_of_anything_is_widened_and_listed_as_new(change, key):
    got = d(m(), m(version="1.1.0", **change))
    assert got["widened"] is True and got["added"][key], got


def test_a_wider_set_by_or_ticket_types_or_a_new_command_is_widened():
    wide = {"mood": {**BASE["fields"]["mood"], "set_by": ["owner", "agent"]}}
    assert d(m(), m(fields=wide))["widened"] is True
    assert d(m(), m(sections=[{**BASE["sections"][0], "types": ["feature", "bug"]}]))["widened"] is True
    assert d(m(), m(entry={"cmd": ["python3", "{pkg}/other.py"]}))["widened"] is True
    assert d(m(), m(agents_md="Use me for everything."))["widened"] is True


def test_fewer_permissions_are_listed_as_removed_and_not_widened():
    got = d(m(), m(version="1.1.0", capabilities=[]))
    assert got["widened"] is False and got["removed"]["capabilities"] == ["network"]
    assert "- network (capabilities, removed)" in got["changes"]
    wide = {"mood": {**BASE["fields"]["mood"], "set_by": ["owner", "agent"]}}
    assert d(m(fields=wide), m())["widened"] is False  # set_by narrowed


def test_a_publisher_key_seen_for_the_first_time_is_widened():
    got = d(m(), m(version="1.0.1"), prev={**PREV, "publisher_key": None})
    assert got["publisher"] == "first_use" and got["widened"] is True
    assert d(m(), m(version="1.0.1"), tofu="unsigned", prev={**PREV, "publisher_key": None})["publisher"] == "unsigned"


def test_an_old_package_that_cannot_be_read_counts_as_widened():
    assert d(None, m(version="1.0.1"))["widened"] is True
