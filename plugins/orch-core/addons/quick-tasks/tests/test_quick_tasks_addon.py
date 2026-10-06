from pathlib import Path

from orch.addons.manifest import load_manifest
from orch.core.quick import ADDON, ADDON_DEFAULTS
from orch_quick_tasks import QuickTasks, create

ADDON_DIR = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON_DIR)


def test_is_only_a_switch_with_settings():
    assert MANIFEST.name == ADDON and MANIFEST.capabilities == {"settings"} and not MANIFEST.binaries


def test_settings_match_what_core_reads():
    defaults = {s.key: s.default for s in MANIFEST.settings_schema}
    assert defaults == ADDON_DEFAULTS  # the manifest's defaults are the ones core falls back on


def test_create_runs_nothing(orch_workspace):
    assert isinstance(create(orch_workspace.context(MANIFEST)), QuickTasks)
