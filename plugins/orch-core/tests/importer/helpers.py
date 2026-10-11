"""Shared helpers of the importer tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.store.helpers import snapshot


def tree(path: Path) -> dict[str, bytes]:
    """Every file under ``path`` (a v1 workspace must come out of an import byte for byte as it went in)."""
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def marker(ws: Any, ref: str) -> dict[str, Any]:
    """The parsed ``v1-import.json`` of an imported ticket, read from the artifact file."""
    uid = ws.uid(ref)
    return json.loads((ws.root / "tickets" / uid / "artifacts" / "v1-import.json").read_bytes())


def types(ws: Any, ref: str) -> list[str]:
    return [e["type"] for e in ws.events(ref)]


def replays_clean(ws: Any) -> Any:
    """A fresh process reads every log and finds nothing wrong; returns its store (closed by the caller)."""
    s = ws.other()
    assert s.chain_errors() == [], s.chain_errors()
    assert [r for r in s.reports if r.code.startswith(("chain", "store", "auth"))] == []
    return s


def with_repo(ws: Any, name: str = "pipelines") -> None:
    """Register a repository in ``settings.repos`` (v1's tickets link ``pipelines``) without counting its prompt."""
    ws.repo(name)
    ws.provider.requests.clear()
    ws.provider.shown.clear()


__all__ = ["marker", "replays_clean", "snapshot", "tree", "types", "with_repo"]
