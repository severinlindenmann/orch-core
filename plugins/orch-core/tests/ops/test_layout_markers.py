"""Step 5: markers, addons, instructions and the ACL/owner check follow the store (``.orch``)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from orch import devsandbox, layout
from orch.addons import install
from orch.host import custody
from orch.host.control import ControlOps
from orch.host.errors import HostError
from orch.instructions import check_targets, write_workspace_files
from orch.store import StoreError
from orch.store.store import open_store_fs

CFG = json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})


def _proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".orch" / ".state").mkdir(parents=True)
    (p / ".orch" / "config.json").write_text(CFG)
    return p


def test_dev_sandbox_marker_is_written_read_and_found_in_the_store(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    assert not devsandbox.marker_present(p)
    devsandbox.write_marker(p, "sbx", "w")
    assert (p / ".orch" / ".state" / devsandbox.WS_MARKER).is_file()
    assert not (p / ".state").exists()
    assert devsandbox.marker_present(p)
    assert devsandbox.read_marker(p)["sandbox_id"] == "sbx"


def test_dev_marker_fails_closed_on_an_unusable_layout(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / "config.json").write_text(CFG)  # two stores
    assert devsandbox.marker_present(p) is True
    with pytest.raises(devsandbox.SandboxRefused):
        devsandbox.read_marker(p)


def test_legacy_marker_still_works(tmp_path: Path) -> None:
    p = tmp_path / "old"
    (p / ".state").mkdir(parents=True)
    (p / "config.json").write_text(CFG)
    devsandbox.write_marker(p, "sbx", "w")
    assert devsandbox.marker_present(p) and (p / ".state" / devsandbox.WS_MARKER).is_file()


def test_bootstrap_probes_read_the_store(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / ".orch" / "events").mkdir()
    assert ControlOps._genesis_landed(str(p), "x") is False  # no log in the store
    (p / ".orch" / "events" / "workspace.jsonl").write_text(json.dumps({"id": "g1"}) + "\n")
    assert ControlOps._foreign_root(str(p), "w", "g1") is False
    assert ControlOps._foreign_root(str(p), "w", "other") is True
    # a decoy in the project root is not the store
    (p / "events").mkdir()
    (p / "events" / "workspace.jsonl").write_text(json.dumps({"id": "decoy"}) + "\n")
    assert ControlOps._foreign_root(str(p), "w", "g1") is False


def test_bootstrap_rollback_removes_dot_orch_only_if_it_is_the_recorded_one(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    made = ControlOps._fingerprint(str(p))
    assert list(made) == [".orch"]
    ControlOps._clean_root(str(p), made)
    assert not (p / ".orch").exists()
    q = _proj(tmp_path / "q")
    ControlOps._clean_root(str(q), {".orch": [1, 2]})  # another inode: not ours
    assert (q / ".orch").is_dir()


def test_addons_live_in_the_store(tmp_path: Path) -> None:
    from orch.addons import builtin

    p = _proj(tmp_path)
    builtin.write_manifest(str(layout.store_of(p)), "demo", b"{}")
    assert (p / ".orch" / "addons" / "demo" / "orch-addon.json").is_file()
    assert not (p / "addons").exists()
    assert install.installed_names(layout.store_of(p)) == ["demo"]


def test_agents_orch_md_goes_to_the_store_and_the_skills_to_the_project(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    store = p / ".orch"
    changed, _ = write_workspace_files(p, store=store, pointers=False)
    assert (store / "AGENTS.orch.md").is_file() and not (p / "AGENTS.orch.md").exists()
    assert ".orch/AGENTS.orch.md" in changed
    assert (p / ".agents" / "skills").is_dir()
    changed2, _ = write_workspace_files(p, store=store, pointers=False)
    assert changed2 == []
    (store / "AGENTS.orch.md").unlink()
    assert write_workspace_files(p, store=store, pointers=False, dry_run=True)[0] == [".orch/AGENTS.orch.md"]
    assert check_targets(p, store) == []


def test_an_agents_orch_md_that_is_a_link_in_the_store_is_refused(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (tmp_path / "t").write_text("x")
    (p / ".orch" / "AGENTS.orch.md").symlink_to(tmp_path / "t")
    assert check_targets(p, p / ".orch")


def test_the_store_dir_owner_and_acl_check_runs_on_the_opened_directory(tmp_path: Path, monkeypatch) -> None:
    p = _proj(tmp_path)
    os.chmod(p / ".orch", 0o700)
    seen = []
    real = custody._check_component

    def spy(fd, path, uid, *, last):
        seen.append((Path(path), last))
        return real(fd, path, uid, last=last)

    monkeypatch.setattr(custody, "_check_component", spy)
    custody.check_workspace_files(tmp_path / "s", "w" * 32, p, os.getuid(), store=".orch")
    assert (p / ".orch", True) in seen
    with pytest.raises(HostError, match="owner"):
        custody.check_workspace_files(tmp_path / "s", "w" * 32, p, os.getuid() + 1, store=".orch")


def test_open_store_fs_never_creates_unless_asked(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(OSError):
        open_store_fs(missing, None, None)
    assert not missing.exists()
    fs, lay = open_store_fs(missing, None, None, create=True)
    fs.close()
    assert missing.is_dir() and lay.legacy  # WRITE_NEW is off
    with pytest.raises(StoreError):
        open_store_fs(missing, (1, 2), None, create=True)
    monkeypatch.setattr(layout, "WRITE_NEW", True)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with pytest.raises(StoreError):  # no create: no .orch made, the open refuses
        open_store_fs(fresh, None, None)
    assert not (fresh / ".orch").exists()
    fs, lay = open_store_fs(fresh, None, None, create=True)
    fs.close()
    assert (fresh / ".orch").is_dir() and not lay.legacy
    shutil.rmtree(fresh / ".orch")
