"""Focus ships as an optional, capability-free built-in."""

import json
from pathlib import Path

from orch.addons import builtin
from orch.addons.manifest import load_manifest
from orch.setup.facts import DEFAULT_ON, OPTIONAL, catalog


def test_focus_manifest():
    data = builtin.manifest_bytes("focus")
    manifest = load_manifest(data)
    assert (manifest.name, manifest.title, manifest.version) == ("focus", "Focus mode", "0.1.0")
    assert manifest.capabilities == []
    assert "orch ask" in manifest.agents_md
    assert "focus" in builtin.NAMES
    repo = Path(__file__).resolve().parents[3] / "addons/focus/orch-addon.json"
    assert repo.read_bytes() == data
    assert json.loads(data)["entry"] == {"cmd": ["orch-focus", "serve"]}


def test_focus_is_optional_and_off_by_default():
    assert "focus" not in DEFAULT_ON
    assert "focus" in OPTIONAL
    assert "focus" not in {row.id for row in catalog()}
    row = next(row for row in catalog(optional=True) if row.id == "focus")
    assert not row.default and not row.needs_runner and row.needs_terminals
