"""Step 4: the custody checks look at the STORE (``.orch``) and its own mode, the host fails closed when the store
layout differs from the registry entry, and the records/instructions helpers take the store directory."""

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
    custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")


@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_a_writable_dot_orch_is_refused(tmp_path: Path, mode: int) -> None:
    project = _tree(tmp_path, mode)
    with pytest.raises(HostError, match=r"\.orch: writable by another principal"):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")


def test_tier_b_dot_orch_0750_passes(tmp_path: Path) -> None:
    project = _tree(tmp_path, 0o750)
    custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")


@pytest.mark.parametrize("name", ["events", "tickets", ".state"])
def test_a_group_writable_dir_below_dot_orch_is_refused(tmp_path: Path, name: str) -> None:
    project = _tree(tmp_path)
    os.chmod(project / ".orch" / name, 0o770)
    with pytest.raises(HostError, match=name):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")


def test_the_checks_do_not_pass_by_looking_at_the_project_root(tmp_path: Path) -> None:
    project = _tree(tmp_path)  # good .orch; bad dirs in the project root are not the store and are not looked at
    os.chmod(project / ".orch" / "events", 0o770)
    (project / "events").mkdir()  # a decoy that is fine
    with pytest.raises(HostError):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")


def test_a_registered_store_that_is_missing_or_linked_is_refused(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    with pytest.raises(HostError, match="missing"):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")
    (tmp_path / "x").mkdir()
    (project / ".orch").symlink_to(tmp_path / "x")
    with pytest.raises(HostError, match="symbolic link"):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store=".orch")
    with pytest.raises(HostError):
        custody.check_workspace_files(tmp_path / "state", "w" * 32, project, UID, store="orch")


def test_store_name_reads_the_disk_and_refuses_a_double_store(tmp_path: Path) -> None:
    import json

    cfg = json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})
    project = tmp_path / "p"
    (project / ".orch").mkdir(parents=True)
    assert custody.store_name(project) is None
    (project / ".orch" / "config.json").write_text(cfg)
    assert custody.store_name(project) == ".orch"
    (project / "config.json").write_text(cfg)
    with pytest.raises(HostError, match="two orch stores"):
        custody.store_name(project)


def _service(lab, store: str | None) -> WorkspaceService:  # noqa: F811
    entry = Entry(WS, "X", lab.root, UID, "g", lab.peers(), store=store)
    return WorkspaceService(entry, lab.state, UID)


def _move_into_dot_orch(root: str) -> None:
    holder = Path(root)
    items = list(holder.iterdir())
    (holder / ".orch").mkdir(mode=0o700)
    for item in items:
        shutil.move(str(item), str(holder / ".orch" / item.name))


def test_the_host_opens_a_store_that_matches_its_entry_and_fails_closed_when_it_does_not(lab) -> None:  # noqa: F811
    legacy_ok = _service(lab, None)
    legacy_ok.open()
    assert legacy_ok.state != "unavailable", legacy_ok.cause
    legacy_ok.executor.shutdown()

    wrong = _service(lab, ".orch")  # the entry says moved; the disk says legacy
    wrong.open()
    assert wrong.state == "unavailable" and wrong.store is None
    wrong.executor.shutdown()

    _move_into_dot_orch(lab.root)  # now the disk says moved
    stale = _service(lab, None)  # an entry that does not know (version skew / half-done migrate)
    stale.open()
    assert stale.state == "unavailable" and stale.store is None
    stale.executor.shutdown()

    moved = _service(lab, ".orch")
    moved.open()
    assert moved.state != "unavailable", moved.cause
    assert moved.store is not None and not moved.store.layout.legacy
    moved.executor.shutdown()


def test_a_registered_legacy_store_is_unavailable_with_the_plain_message_not_a_crash(lab) -> None:  # noqa: F811
    from orch import layout

    layout.LEGACY_ALLOWED = False  # the shipped refusal (the suite's fixture reset puts the flag back)
    svc = _service(lab, None)
    svc.open()
    try:
        assert svc.state == "unavailable" and svc.store is None
        assert "Delete config.json" in (svc.cause or "") and "run orch setup again" in (svc.cause or "")
    finally:
        svc.executor.shutdown()
