"""Discovery reads ``<project>/.orch``; a store from an older version in the project root is found (so the person is
told to delete it) but never read as a store. These tests build the stores by hand."""

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


def test_an_old_root_store_is_found_but_never_read_as_a_store(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")
    found = discovery.locate(project, explicit=True)
    assert found is not None and found.layout is None and found.project == project.resolve()
    assert found.store == project.resolve() / ".orch"  # where a store would be, not the old files
    assert discovery.read_config(project) is None
    with pytest.raises(StoreError, match="older version.*run orch setup again"):
        Store.open(project, expected_workspace_id="w" * 32, load="lazy")


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


def test_read_config_reads_dot_orch_only(tmp_path: Path) -> None:
    _store(tmp_path / "old")
    _store(tmp_path / "new" / ".orch")
    assert discovery.read_config(tmp_path / "old") is None
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
    shutil.copytree(ws.store_dir, project / ".orch", symlinks=True)
    wid = json.loads((ws.store_dir / "config.json").read_text())["workspace"]["id"]
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


def test_store_open_on_a_project_puts_the_store_in_dot_orch(ws) -> None:
    wid = json.loads((ws.store_dir / "config.json").read_text())["workspace"]["id"]
    store = Store.open(ws.root, expected_workspace_id=wid, load="lazy")
    try:
        assert store.layout == Layout.new_at(ws.root)
    finally:
        store.close()


def _project_with_store(ws, tmp_path: Path) -> tuple[Path, str]:
    project = tmp_path / "proj"
    project.mkdir()
    shutil.copytree(ws.store_dir, project / ".orch", symlinks=True)
    return project, json.loads((ws.store_dir / "config.json").read_text())["workspace"]["id"]


def test_an_inode_swap_of_the_store_dir_between_discovery_and_open_is_refused(ws, tmp_path: Path) -> None:
    project, wid = _project_with_store(ws, tmp_path)
    found = discovery.locate(project, explicit=False)
    assert found is not None and found.store_pin is not None and found.store_pin.ident is not None
    (project / ".orch").rename(project / ".orch-old")
    shutil.copytree(project / ".orch-old", project / ".orch", symlinks=True)  # same content, another inode
    with pytest.raises(StoreError, match="no longer the store directory"):
        Store.open(project, expected_workspace_id=wid, ident=found.ident, store_pin=found.store_pin)
    # the very directory that was found opens
    shutil.rmtree(project / ".orch")
    (project / ".orch-old").rename(project / ".orch")
    Store.open(project, expected_workspace_id=wid, ident=found.ident, store_pin=found.store_pin).close()


def test_the_store_layout_changing_under_a_found_workspace_is_refused(ws, tmp_path: Path) -> None:
    project, wid = _project_with_store(ws, tmp_path)
    found = discovery.locate(project, explicit=False)
    assert found is not None
    shutil.move(project / ".orch" / "config.json", project / "config.json")  # now an old-version root store
    shutil.rmtree(project / ".orch")
    with pytest.raises(StoreError, match="older version"):
        Store.open(project, expected_workspace_id=wid, ident=found.ident, store_pin=found.store_pin)


def test_an_implicit_walk_pin_rechecks_ownership_of_the_opened_store_dir(ws, tmp_path: Path) -> None:
    project, wid = _project_with_store(ws, tmp_path)
    found = discovery.locate(project, explicit=False)
    assert found is not None and found.store_pin is not None and found.store_pin.owned
    os.chmod(project / ".orch", 0o770)  # group-writable after discovery: same inode, no longer private
    with pytest.raises(StoreError, match="no longer the store directory"):
        Store.open(project, expected_workspace_id=wid, ident=found.ident, store_pin=found.store_pin)


def test_a_project_that_is_not_private_is_no_candidate_for_a_dot_orch_in_the_walk(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _store(project / ".orch")
    os.chmod(project, 0o777 & ~0o002 | 0o020)  # group-writable project
    assert discovery.locate(project, explicit=False) is None
    assert discovery.locate(project, explicit=True) is not None  # an explicit path is the person's choice


def test_a_strangers_dot_orch_does_not_block_the_walk_but_the_persons_own_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _store(tmp_path / "outer")
    inner = outer / "inner"
    inner.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (inner / ".orch").symlink_to(tmp_path / "elsewhere")  # a link where the walk passes
    with pytest.raises(LayoutError):
        discovery.locate(inner, explicit=False)  # ours: fail closed
    monkeypatch.setattr(discovery, "_owner", lambda: os.getuid() + 1)  # now it is someone else's: skipped, no error
    assert discovery.locate(inner, explicit=False) is None  # (nothing of "ours" is left; the point is: no raise)
    with pytest.raises(LayoutError):
        discovery.locate(inner, explicit=True)  # an explicit lookup still refuses


def _served(project: Path) -> bool:
    from orch.host.service import is_served

    return is_served(project)


def test_the_served_marker_lives_in_the_store_and_the_guard_holds(ws, tmp_path: Path) -> None:
    from orch.host.service import SERVED_MARKER, served_marker
    from orch.records.direct import DirectLink

    project, wid = _project_with_store(ws, tmp_path)
    assert not _served(project)
    served_marker(str(project), True, tmp_path)
    assert (project / ".orch" / ".state" / SERVED_MARKER).exists()
    assert not (project / ".state").exists()
    assert _served(project)
    link = DirectLink(project, wid, tmp_path / "state")
    from orch.records import RecordsError

    with pytest.raises(RecordsError) as exc:
        link._open()
    assert exc.value.code == "host.required"
    served_marker(str(project), False, tmp_path)
    assert not _served(project)
    # a layout that cannot be placed counts as served (fail closed)
    shutil.move(project / ".orch", tmp_path / "moved")
    (project / ".orch").symlink_to(tmp_path / "moved")
    assert _served(project)


def test_a_non_orch_dot_orch_away_from_the_start_does_not_block_the_walk(tmp_path: Path) -> None:
    outer = _store(tmp_path / "outer")
    inner = outer / "inner"
    sub = inner / "sub"
    sub.mkdir(parents=True)
    (inner / ".orch").write_text("another tool's file")  # e.g. ~/.orch of something else
    found = discovery.locate(sub, explicit=False)
    assert found is not None and found.project == outer.resolve()
    with pytest.raises(LayoutError) as exc:  # standing in the directory that holds it: refused, with its own hint
        discovery.locate(inner, explicit=False)
    assert "migrate" not in (exc.value.hint or "") and ".orch" in (exc.value.hint or "")


def test_a_directory_dot_orch_without_an_orch_config_is_passed_by_the_walk(tmp_path: Path) -> None:
    outer = _store(tmp_path / "outer")
    (outer / "inner" / ".orch").mkdir(parents=True)  # another tool's directory
    (outer / "inner" / ".orch" / "config.json").write_text('{"name": "x"}')
    found = discovery.locate(outer / "inner", explicit=False)
    assert found is not None and found.project == outer.resolve()


def test_runtime_gives_the_double_store_hint_only_for_two_stores(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")
    (project / ".orch").write_text("x")
    with pytest.raises(OrchError) as exc:
        runtime.discover({"ORCH_WORKSPACE": str(project)}, None, FILES)
    assert "migrate" not in (exc.value.hint or "")


def test_host_mode_refuses_a_linked_or_file_dot_orch_like_direct_mode(tmp_path: Path) -> None:
    for kind in ("symlink", "file"):
        project = _store(tmp_path / kind)  # a legacy store next to it must not be used
        if kind == "symlink":
            (tmp_path / "elsewhere").mkdir(exist_ok=True)
            (project / ".orch").symlink_to(tmp_path / "elsewhere")
        else:
            (project / ".orch").write_text("x")
        files = Files.host((str(project),))
        try:
            with pytest.raises(OrchError, match="not a real directory"):
                runtime.discover({"ORCH_WORKSPACE": str(project)}, None, files)
        finally:
            files.close()


def test_store_open_never_makes_a_dot_orch_that_vanished_after_the_resolve(
    ws, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from orch import layout as lay

    project, wid = _project_with_store(ws, tmp_path)
    monkeypatch.setattr(lay, "open_store_dir", lambda *a, **k: None)  # resolve saw it, the open does not
    # (resolve itself uses open_store_dir; patch only the second use by counting)
    calls = {"n": 0}
    real = lay.resolve

    def resolve(fd, project_path):
        calls["n"] += 1
        monkeypatch.undo()
        out = real(fd, project_path)
        monkeypatch.setattr(lay, "open_store_dir", lambda *a, **k: None)
        return out

    monkeypatch.setattr(lay, "resolve", resolve)
    with pytest.raises(StoreError, match="vanished"):
        Store.open(project, expected_workspace_id=wid, load="lazy")
    assert (project / ".orch").is_dir()  # never made again, never replaced


def test_a_linked_dot_orch_next_to_a_legacy_config_still_refuses_away_from_the_start(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")  # a legacy store
    (project / ".orch").write_text("x")  # and a file named .orch beside it: the double-store case
    sub = project / "sub"
    sub.mkdir()
    with pytest.raises(LayoutError, match="not a real directory"):
        discovery.locate(sub, explicit=False)


def test_records_and_stale_findings_use_the_store_dir(ws, tmp_path: Path) -> None:
    from orch.instructions import stale_findings
    from orch.ops.runtime import records_dir

    project, _ = _project_with_store(ws, tmp_path)
    layout = Layout.new_at(project)
    assert records_dir(layout) == project / ".orch" / ".state" / "sessions"
    findings = stale_findings(project, project / ".orch")
    assert {"where": "AGENTS.orch.md", "what": "missing"} in findings  # looked for in the store dir...
    (project / "AGENTS.orch.md").write_text("x")  # ...a copy in the project root is not it
    assert {"where": "AGENTS.orch.md", "what": "missing"} in stale_findings(project, project / ".orch")
    assert any(f["where"].startswith(".agents/skills") for f in findings)  # skills stay under the project


def test_hosted_discovery_does_not_read_a_config_in_a_dot_orch_folder_as_an_old_root_store(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _store(project / ".orch")
    (project / ".orch" / "tickets").mkdir()
    files = Files.host((str(project),))
    try:
        found = runtime.discover({}, project / ".orch" / "tickets", files)
        assert found is not None and found.path == project and found.layout == Layout.new_at(project)
        found = runtime.discover({"ORCH_WORKSPACE": str(project / ".orch")}, None, files)
        assert found is not None and found.path == project
    finally:
        files.close()


def test_direct_discovery_refuses_an_old_root_store_unless_the_caller_only_names_it(tmp_path: Path) -> None:
    project = _store(tmp_path / "p")
    with pytest.raises(OrchError, match="older version"):
        runtime.discover({"ORCH_WORKSPACE": str(project)}, None, FILES)
    found = runtime.discover({"ORCH_WORKSPACE": str(project)}, None, FILES, allow_old_store=True)
    assert found is not None and found.layout is None
