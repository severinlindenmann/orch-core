"""A package builder for the addon tests: the fixture addon plus a manifest that runs it with this interpreter."""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"

MANIFEST: dict[str, Any] = {
    "schema": "orch.addon/2",
    "name": "echo",
    "version": "1.0.0",
    "title": "Echo",
    "entry": {"cmd": [sys.executable, "{pkg}/addon.py"]},
    "fields": {
        "points": {
            "type": "integer",
            "min": 0,
            "max": 100,
            "set_by": ["owner", "maintainer", "agent", "addon"],
            "gate": ["plan"],
            "show": True,
            "filter": True,
        },
        "note": {"type": "string", "max_len": 50, "set_by": ["addon"]},
        "mood": {"type": "enum", "values": ["good", "bad"], "set_by": ["owner"], "gate": ["verify", "plan"]},
        "tags": {"type": "string_list", "max_items": 3, "set_by": ["agent", "maintainer"]},
        "reviewer": {"type": "person", "set_by": ["owner"]},
        "done": {"type": "boolean", "set_by": ["agent"]},
        "memo": {"type": "text", "max_len": 100, "set_by": ["owner", "addon"]},
    },
    "sections": [
        {"id": "notes", "heading": "Echo notes", "after": "plan", "gate": ["plan"], "types": ["feature", "bug"]},
        {"id": "extra", "heading": "Echo extra", "after": "context", "types": ["feature"]},
    ],
    "artifact_kinds": [{"kind": "chart", "label": "Chart"}],
    "capabilities": ["serve_http"],
}


def manifest(**over: Any) -> dict[str, Any]:
    m = copy.deepcopy(MANIFEST)
    m.update(over)
    return m


def make_package(
    root: Path,
    name: str = "echo",
    *,
    man: dict[str, Any] | None = None,
    files: dict[str, bytes] | None = None,
    script: bool = True,
) -> Path:
    """``<root>/addons/<name>/`` with the manifest and (unless ``script`` is false) the fixture addon."""
    d = root / "addons" / name
    d.mkdir(parents=True)
    m = copy.deepcopy(man if man is not None else MANIFEST)
    m["name"] = name if man is None else m.get("name", name)
    (d / "orch-addon.json").write_text(json.dumps(m))
    if script:
        shutil.copy(FIXTURES / "echo" / "addon.py", d / "addon.py")
    for rel, data in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d
