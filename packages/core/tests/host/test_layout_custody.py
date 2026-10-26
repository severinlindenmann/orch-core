"""The custody checks look at the STORE (``.orch``) and its own mode, and the host serves a root that holds an
old-version store as unavailable with the plain delete message."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from orch.host import custody
from orch.host.errors import HostError
from orch.host.registry import Entry
from orch.host.workspace import WorkspaceService
from tests.host.human_helpers import lab  # noqa: F401  (fixture)
from tests.store.helpers import WS

UID = os.getuid()


def _tree(tmp_path: Path, mode: int = 0o700) -> Path:
    project = tmp_path / "proj"
    for d in ("events", "tickets", ".state"):
        (project / ".orch" / d).mkdir(parents=True)
    os.chmod(project / ".orch", mode)
    return project


def test_a_private_dot_orch_passes(tmp_path: Path) -> None:
    project = _tree(tmp_path)
    custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_writable_dot_orch_is_refused(tmp_path: Path, mode: int) -> None:
    project = _tree(tmp_path, mode)
    with pytest.raises(HostError, match=r"\.orch: writable by another principal"):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)


def test_tier_b_dot_orch_0750_passes(tmp_path: Path) -> None:
    project = _tree(tmp_path, 0o750)
    custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)


@pytest.mark.parametrize("name", ["events", "tickets", ".state"])
def test_a_group_writable_dir_below_dot_orch_is_refused(tmp_path: Path, name: str) -> None:
    project = _tree(tmp_path)
    os.chmod(project / ".orch" / name, 0o770)
    with pytest.raises(HostError, match=name):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)


def test_the_checks_do_not_pass_by_looking_at_the_project_root(tmp_path: Path) -> None:
    project = _tree(tmp_path)  # good .orch; bad dirs in the project root are not the store and are not looked at
    os.chmod(project / ".orch" / "events", 0o770)
    (project / "events").mkdir()  # a decoy that is fine
    with pytest.raises(HostError):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)


def test_a_registered_store_that_is_missing_or_linked_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(HostError, match="missing"):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)
    (tmp_path / "x").mkdir()
    (project / ".orch").symlink_to(tmp_path / "x")
    with pytest.raises(HostError, match="symbolic link"):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID)


def test_store_of_reads_the_disk_and_refuses_a_double_store_and_an_old_store(tmp_path: Path) -> None:
    import json

    from orch import layout

    cfg = json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})
    project = tmp_path / "p"
    (project / ".orch").mkdir(parents=True)
    (project / ".orch" / "config.json").write_text(cfg)
    assert layout.store_of(project) == project / ".orch"
    (project / "config.json").write_text(cfg)
    with pytest.raises(layout.LayoutError, match="two orch stores"):
        layout.store_of(project)
    (project / ".orch" / "config.json").unlink()
    with pytest.raises(layout.LegacyStoreError, match="Delete config.json"):
        layout.store_of(project)


def _service(lab) -> WorkspaceService:  # noqa: F811
    entry = Entry(WS, "X", lab.root, UID, "g", lab.peers())
    return WorkspaceService(entry, lab.state, UID)


def _move_out_of_dot_orch(root: str) -> None:
    """What an older version left: the store files directly in the project root."""
    holder = Path(root)
    for item in list((holder / ".orch").iterdir()):
        shutil.move(str(item), str(holder / item.name))
    (holder / ".orch").rmdir()


def test_the_host_opens_its_store(lab) -> None:  # noqa: F811
    moved = _service(lab)
    moved.open()
    assert moved.state != "unavailable", moved.cause
    assert moved.store is not None and moved.store.layout.store == Path(lab.root) / ".orch"
    moved.executor.shutdown()


def test_a_registered_legacy_store_is_unavailable_with_the_plain_message_not_a_crash(lab) -> None:  # noqa: F811
    _move_out_of_dot_orch(lab.root)
    svc = _service(lab)
    svc.open()
    try:
        assert svc.state == "unavailable" and svc.store is None
        assert "Delete config.json" in (svc.cause or "") and "run orch setup again" in (svc.cause or "")
    finally:
        svc.executor.shutdown()


@pytest.mark.parametrize(
    "problem",
    ["ok", "directory_mode", "file_mode", "link", "nested", "owner", "dir_link", "parent_mode", "second_link"],
)
def test_addon_credential_custody(tmp_path, problem):
    project = _tree(tmp_path)
    state = tmp_path / "state"
    github = state / "hosts" / "ws" / "addons" / "demo"
    github.mkdir(parents=True, mode=0o700)
    github.parent.chmod(0o700)
    item = github / "credential"
    item.write_text("synthetic")
    item.chmod(0o600)
    if problem == "directory_mode":
        github.chmod(0o755)
    elif problem == "file_mode":
        item.chmod(0o644)
    elif problem == "link":
        item.unlink()
        item.symlink_to(tmp_path / "absent")
    elif problem == "nested":
        (github / "nested").mkdir(mode=0o700)
    elif problem == "dir_link":
        github.rename(github.with_name("elsewhere"))
        github.symlink_to(github.with_name("elsewhere"))
    elif problem == "parent_mode":
        github.parent.chmod(0o755)
    elif problem == "second_link":
        os.link(item, tmp_path / "second")
    if problem == "ok":
        custody.check_workspace_files(state, "ws", project, UID)
    else:
        with pytest.raises(HostError) as caught:
            custody.check_workspace_files(state, "ws", project, UID + (problem == "owner"))
        assert caught.value.code == "custody.unsafe_mode"
