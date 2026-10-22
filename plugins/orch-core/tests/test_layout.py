"""``orch.layout``: the Layout value, ``resolve`` (both layouts, the double store, a linked ``.orch``)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orch import layout
from orch.layout import Layout, LayoutError, resolve

CFG = json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})


def _resolve(project: Path) -> Layout | None:
    fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return resolve(fd, project)
    finally:
        os.close(fd)


def test_the_store_dir_is_dot_orch() -> None:
    assert layout.STORE_DIR == ".orch"
    assert not hasattr(layout, "WRITE_NEW") and not hasattr(layout, "LEGACY_ALLOWED")


def test_layout_values(tmp_path: Path) -> None:
    new = Layout.new_at(tmp_path)
    assert (new.project, new.store) == (tmp_path, tmp_path / ".orch")
    assert layout.fresh(tmp_path) == new


def test_resolve_new_none_and_the_old_store(tmp_path: Path) -> None:
    assert _resolve(tmp_path) is None
    (tmp_path / "config.json").write_text(CFG)
    with pytest.raises(layout.LegacyStoreError, match="older version"):
        _resolve(tmp_path)  # a root store is named and refused, never read
    (tmp_path / "config.json").unlink()
    (tmp_path / ".orch").mkdir()
    assert _resolve(tmp_path) is None  # the project's own .orch is not a store
    (tmp_path / ".orch" / "config.json").write_text(CFG)
    assert _resolve(tmp_path) == Layout.new_at(tmp_path)


def test_other_config_json_is_not_a_store(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text('{"name": "a js project"}')
    (tmp_path / ".orch").mkdir()
    (tmp_path / ".orch" / "config.json").write_text(CFG)
    assert _resolve(tmp_path) == Layout.new_at(tmp_path)


def test_two_stores_refused(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(CFG)
    (tmp_path / ".orch").mkdir()
    (tmp_path / ".orch" / "config.json").write_text(CFG)
    with pytest.raises(LayoutError, match="two orch stores"):
        _resolve(tmp_path)


def test_linked_store_dir_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "config.json").write_text(CFG)
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".orch").symlink_to(real)
    with pytest.raises(LayoutError):
        _resolve(proj)


def test_store_dir_that_is_a_file_is_refused_not_ignored(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(CFG)
    (tmp_path / ".orch").write_text("not a directory")
    with pytest.raises(LayoutError, match="not a real directory"):
        _resolve(tmp_path)


def test_linked_store_dir_is_refused_even_with_a_legacy_store(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(CFG)
    (tmp_path / "x").mkdir()
    (tmp_path / ".orch").symlink_to(tmp_path / "x")
    with pytest.raises(LayoutError):
        _resolve(tmp_path)
