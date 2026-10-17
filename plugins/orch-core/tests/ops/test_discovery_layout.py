"""Discovery reads both layouts (design B11-P2 step 2): the legacy root store and ``<project>/.orch``. Nothing writes
``.orch`` yet; these tests build it by hand."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from orch import discovery
from orch.layout import Layout, LayoutError
from orch.ops import runtime
from orch.ops.caps import Files
from orch.ops.errors import OrchError
from orch.store import Store, StoreError

CFG = json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})
FILES = Files.direct()


def _store(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(CFG)
    return path


def test_finds_the_new_layout_from_the_project_and_below(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _store(project / ".orch")
    (project / "src" / "deep").mkdir(parents=True)
    for start in (project, project / "src" / "deep"):
        found = discovery.locate(start, explicit=False)
        assert found is not None
        assert found.project == project.resolve()
        assert found.store == project.resolve() / ".orch"
        assert found.layout == Layout.new_at(project.resolve())
        assert found.config["workspace"]["id"] == "w"


def test_finds_the_legacy_layout(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")
    found = discovery.locate(project, explicit=True)
    assert found is not None and found.store == found.project == project.resolve()


def test_the_v2_orchestrator_subfolder_is_no_longer_probed(tmp_path: Path) -> None:
    _store(tmp_path / "p" / "orchestrator")
    assert discovery.locate(tmp_path / "p", explicit=True) is None
    assert discovery.locate(tmp_path / "p", explicit=False) is None


def test_explicit_alias_names_the_store_dir_of_a_confirmed_store(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _store(project / ".orch")
    found = discovery.locate(project / ".orch", explicit=True)
    assert found is not None and found.project == project.resolve()
    # a .orch that is not an orch store is no alias, and falls back to "the path itself": not a workspace
    other = tmp_path / "q"
    (other / ".orch").mkdir(parents=True)
    assert discovery.locate(other / ".orch", explicit=True) is None


def test_two_stores_are_refused(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")
    _store(project / ".orch")
    for explicit in (True, False):
        with pytest.raises(LayoutError, match="two orch stores"):
            discovery.locate(project, explicit=explicit)


@pytest.mark.parametrize("kind", ["symlink", "file"])
def test_a_store_dir_that_is_not_a_real_directory_never_falls_through(tmp_path: Path, kind: str) -> None:
    project = _store(tmp_path / "p")  # a legacy store next to it must not be used
    if kind == "symlink":
        (tmp_path / "elsewhere").mkdir()
        (project / ".orch").symlink_to(tmp_path / "elsewhere")
    else:
        (project / ".orch").write_text("x")
    with pytest.raises(LayoutError, match="not a real directory"):
        discovery.locate(project, explicit=True)


def test_runtime_maps_the_refusal_to_a_named_error(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")
    _store(project / ".orch")
    with pytest.raises(OrchError, match="two orch stores"):
        runtime.discover({"ORCH_WORKSPACE": str(project)}, None, FILES)


def test_read_config_reads_either_layout(tmp_path: Path) -> None:
    _store(tmp_path / "old")
    _store(tmp_path / "new" / ".orch")
    assert discovery.read_config(tmp_path / "old")["workspace"]["id"] == "w"
    assert discovery.read_config(tmp_path / "new")["workspace"]["id"] == "w"
    assert discovery.read_config(tmp_path) is None


def test_workspace_exposes_project_and_store(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _store(project / ".orch")
    ws = runtime.Workspace({"ORCH_WORKSPACE": str(project)}, files=FILES)
    assert ws.project == project.resolve()
    assert ws.root == project.resolve()
    assert ws.store_dir == project.resolve() / ".orch"
    assert ws.layout == Layout.new_at(project.resolve())


def test_store_open_resolves_the_new_layout_and_refuses_a_linked_one(ws, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    shutil.copytree(ws.root, project / ".orch", symlinks=True)
    wid = json.loads((ws.root / "config.json").read_text())["workspace"]["id"]
    store = Store.open(project, expected_workspace_id=wid, load="lazy")
    try:
        assert store.layout == Layout.new_at(project)
        assert store.root == project / ".orch"
        assert store.state is not None
    finally:
        store.close()
    shutil.move(project / ".orch", tmp_path / "moved")
    os.symlink(tmp_path / "moved", project / ".orch")
    with pytest.raises(StoreError) as exc:
        Store.open(project, expected_workspace_id=wid, load="lazy")
    assert exc.value.code == "validation.path"


def test_store_open_on_a_legacy_root_keeps_project_equal_store(ws) -> None:
    wid = json.loads((ws.root / "config.json").read_text())["workspace"]["id"]
    store = Store.open(ws.root, expected_workspace_id=wid, load="lazy")
    try:
        assert store.layout == Layout.legacy_at(ws.root)
    finally:
        store.close()
