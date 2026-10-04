"""orch-core from any marketplace (issue 1): doctor accepts it, init/sync keep it or derive it, never add a second."""
import json
import os
from pathlib import Path

import pytest

from orch.instructions import settings as st
from orch.instructions.settings import PLUGIN_ID, merge_settings, resolve_plugin_id

CFG = {"commit": {"forbid_attribution": True}}


@pytest.fixture(autouse=True)
def no_plugin_root(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


def _installed(entries: dict) -> None:
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "plugins": entries}), encoding="utf-8")


def test_an_already_enabled_id_from_another_marketplace_is_kept_alone():
    merged = merge_settings({"enabledPlugins": {"orch-core@orch-core": True}}, CFG, plugin_mode=True)
    assert merged["enabledPlugins"] == {"orch-core@orch-core": True}
    assert "extraKnownMarketplaces" not in merged


def test_a_disabled_id_is_respected_and_no_second_copy_is_added():
    merged = merge_settings({"enabledPlugins": {"orch-core@orch-core": False}}, CFG, plugin_mode=True)
    assert merged["enabledPlugins"] == {"orch-core@orch-core": False}


@pytest.mark.parametrize("root, expected", [
    ("/Users/x/.claude/plugins/cache/orch-core/orch-core/0.1.0", "orch-core@orch-core"),
    ("/Users/x/.claude/plugins/marketplaces/my-fork/plugins/orch-core", "orch-core@my-fork"),
    ("/Users/x/.claude/plugins/cache/team-store/orch-core/0.1.0", "orch-core@team-store"),
])
def test_the_running_plugin_folder_names_the_marketplace(monkeypatch, root, expected):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", root)
    assert resolve_plugin_id() == expected


def test_a_cli_outside_the_plugin_uses_the_one_installed_id(tmp_path):
    _installed({"orch-core@orch-core": [{"scope": "user", "installPath": "/x"}],
                "other@orch-core": [{"scope": "user"}]})
    merged = merge_settings({}, CFG, plugin_mode=True, root=tmp_path)
    assert merged["enabledPlugins"] == {"orch-core@orch-core": True}
    assert merged["extraKnownMarketplaces"] == {"orch-core": {"source": {"source": "github", "repo": "severinlindenmann/orch-core"}}}


def test_project_scope_counts_only_for_its_own_project(tmp_path):
    _installed({"orch-core@my-fork": [{"scope": "project", "projectPath": str(tmp_path / "elsewhere")}]})
    assert resolve_plugin_id(tmp_path) == PLUGIN_ID
    _installed({"orch-core@my-fork": [{"scope": "project", "projectPath": str(tmp_path)}]})
    assert resolve_plugin_id(tmp_path) == "orch-core@my-fork"


def test_two_installed_ids_or_none_fall_back_to_the_store(tmp_path):
    assert resolve_plugin_id(tmp_path) == PLUGIN_ID
    _installed({"orch-core@orch-core": [{"scope": "user"}], "orch-core@my-fork": [{"scope": "user"}]})
    assert resolve_plugin_id(tmp_path) == PLUGIN_ID
    merged = merge_settings({}, CFG, plugin_mode=True, root=tmp_path)
    assert merged["extraKnownMarketplaces"] == {st.MARKETPLACE: {"source": st.MARKETPLACE_SOURCE}}


def test_an_unknown_marketplace_gets_no_extra_marketplace_entry(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/h/.claude/plugins/marketplaces/local-dev/plugins/orch-core")
    merged = merge_settings({}, CFG, plugin_mode=True)
    assert merged["enabledPlugins"] == {"orch-core@local-dev": True} and "extraKnownMarketplaces" not in merged




def test_doctor_accepts_orch_core_from_any_marketplace(tmp_path):
    from orch.onboarding import _enabled_plugin_id, _plugin_enabled_in
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"enabledPlugins": {"orch-core@orch-core": True, "orch-corex@y": True}}))
    assert _plugin_enabled_in(path) and _enabled_plugin_id(path) == "orch-core@orch-core"
    path.write_text(json.dumps({"enabledPlugins": {"orch-core@orch-core": False, "orch-corex@y": True}}))
    assert not _plugin_enabled_in(path)
