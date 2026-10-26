"""The install state file (``<state dir>/install/install.json``) and the directories under it. The state dir comes
from :func:`orch.ops.runtime.state_dir` (never the real ``~/.config/orch`` in tests: they set HOME)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orch.ops.runtime import state_dir

__all__ = ["Layout", "load", "save"]


class Layout:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env
        self.root = state_dir(env) / "install"
        self.tools = self.root / "tools"
        self.bin = self.root / "bin"
        self.logs = self.root / "logs"
        self.file = self.root / "install.json"
        home = env.get("HOME")
        self.home = Path(home) if home else Path.home()

    def data(self, part: str) -> Path:
        return self.root / "parts" / part / "data"

    def ensure(self) -> None:
        for d in (self.tools, self.bin, self.logs):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)


def load(layout: Layout) -> dict[str, Any]:
    try:
        return json.loads(layout.file.read_text())
    except (OSError, ValueError):
        return {"v": 1, "address": None, "domain": None, "parts": {}, "workspaces": []}


def save(layout: Layout, st: dict[str, Any]) -> None:
    layout.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = layout.file.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, layout.file)
