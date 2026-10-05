import json

import pytest

from addon_fixtures import GOOD
from orch.addons.manifest import load_manifest, manifest_problems, parse_manifest
from orch.errors import ValidationError


def test_good_manifest_parses():
    m = parse_manifest(GOOD)
    assert m.name == "hello-status" and m.entry_package == "hello_status" and m.entry_factory == "create"
    assert m.capabilities == frozenset({"provider", "page", "settings"}) and m.has("page")
    assert m.defaults() == {"greeting": "Hello"}
    assert m.permissions() == {"capabilities": ["page", "provider", "settings"], "binaries": ["git"],
                               "env": [], "requires_api": "2", "actions": [], "uploads": [],
                               "remote_humans": False, "remote_actions": []}


def test_setting_binary_and_actions():
    data = {**GOOD, "capabilities": [*GOOD["capabilities"], "panel"], "slots": ["ticket.code"],
            "binaries": ["git", "setting:cli_path"],
            "settings_schema": [*GOOD["settings_schema"], {"key": "cli_path", "label": "CLI", "type": "text"}],
            "actions": [{"id": "rerun", "label": "Rerun failed", "confirm": "Rerun the failed checks?"}]}
    m = parse_manifest(data)
    assert m.bare_binaries() == ("git",) and m.setting_binaries() == ("cli_path",)
    assert m.action("rerun").confirm == "Rerun the failed checks?" and m.action("nope") is None


@pytest.mark.parametrize("patch, needle", [
    ({"name": "Hello"}, "name"),
    ({"name": "a/b"}, "name"),
    ({"version": "1.0"}, "version"),
    ({"requires_api": "3"}, "requires_api '3' is not supported"),
    ({"kind": "process"}, "kind"),
    ({"capabilities": ["provider", "cli"]}, "capabilities"),
    ({"binaries": ["/bin/sh"]}, "binaries"),
    ({"binaries": ["setting:missing"]}, "setting:missing"),
    ({"env": ["gh_host"]}, "env"),
    ({"env": ["ORCH_HARNESS"]}, "ORCH_"),
    ({"entry": "hello_status"}, "entry"),
    ({"menu": {"title": "X", "icon": "<svg>"}}, "menu.icon"),
    ({"settings_schema": [{"key": "token", "label": "Token", "type": "text", "secret": True}]}, "secret"),
    ({"settings_schema": [{"key": "mode", "label": "Mode", "type": "select"}]}, "options"),
    ({"capabilities": ["provider", "page"]}, "settings_schema"),
    ({"slots": ["ticket.code"]}, "panel"),
    ({"capabilities": [*GOOD["capabilities"], "panel"], "slots": ["today.everything"]}, "slots"),
    ({"actions": [{"id": "Rerun", "label": "Rerun"}]}, "actions"),
    ({"extra": 1}, "unknown key"),
])
def test_bad_manifests_name_the_problem(patch, needle):
    data = {**GOOD, **patch}
    problems = manifest_problems(data)
    assert any(needle in p for p in problems), problems
    with pytest.raises(ValidationError):
        parse_manifest(data)


def test_page_needs_a_menu():
    data = dict(GOOD)
    data.pop("menu")
    assert any("menu" in p for p in manifest_problems(data))


def test_not_an_object():
    assert manifest_problems([]) == ["the manifest must be a JSON object"]


def test_load_manifest(tmp_path):
    with pytest.raises(ValidationError, match="no orch-addon.json"):
        load_manifest(tmp_path)
    (tmp_path / "orch-addon.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValidationError, match="not valid JSON"):
        load_manifest(tmp_path)
    (tmp_path / "orch-addon.json").write_text(json.dumps(GOOD), encoding="utf-8")
    assert load_manifest(tmp_path).title == "Hello status"


@pytest.mark.parametrize("name", ["changed", "addons", "core", "orch"])
def test_reserved_names_are_refused(name):
    assert any("reserved" in p for p in manifest_problems({**GOOD, "name": name}))
