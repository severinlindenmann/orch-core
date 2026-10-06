from pathlib import Path

from orch.addons.manifest import load_manifest
from orch_graph import Graph, create

ADDON = Path(__file__).resolve().parents[1]
MANIFEST = load_manifest(ADDON)


def test_is_only_a_switch():
    assert MANIFEST.name == "graph" and not MANIFEST.capabilities and not MANIFEST.binaries


def test_create_runs_nothing(orch_workspace):
    assert isinstance(create(orch_workspace.context(MANIFEST)), Graph)
