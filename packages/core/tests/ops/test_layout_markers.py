"""Step 5: markers, addons, instructions and the ACL/owner check follow the store (``.orch``)."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from orch import devsandbox, layout
from orch.addons import install
from orch.host import custody
from orch.host.control import ControlOps
from orch.host.errors import HostError
from orch.instructions import check_targets, write_workspace_files
from orch.store import Store, StoreError
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


def test_a_root_store_is_refused_and_counts_as_marked(tmp_path: Path) -> None:
    p = tmp_path / "old"
    (p / ".state").mkdir(parents=True)
    (p / "config.json").write_text(CFG)
    assert devsandbox.marker_present(p) is True  # an old-version store is refused: fail closed
    with pytest.raises(devsandbox.SandboxRefused):
        devsandbox.write_marker(p, "sbx", "w")
    assert not (p / ".state" / devsandbox.WS_MARKER).exists()


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
    st = os.stat(p / ".orch")
    made = ControlOps._fingerprint(str(p), (st.st_dev, st.st_ino))
    assert made == {".orch": [st.st_dev, st.st_ino]}
    ControlOps._clean_root(str(p), made)
    assert not (p / ".orch").exists()
    assert not [n for n in os.listdir(p) if n.startswith(".orch-rollback")]  # no tombstone left
    q = _proj(tmp_path / "q")
    ControlOps._clean_root(str(q), {".orch": [1, 2]})  # another inode: not ours
    assert (q / ".orch").is_dir() and (q / ".orch" / "config.json").is_file()
    assert ControlOps._fingerprint(str(q), None) == {}  # an open that made nothing records nothing
    ControlOps._clean_root(str(q), {})
    assert (q / ".orch").is_dir()


def test_rollback_never_follows_a_link_or_a_swapped_dot_orch(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    st = os.stat(p / ".orch")
    made = {".orch": [st.st_dev, st.st_ino]}
    (p / ".orch").rename(p / "moved")
    (p / ".orch").symlink_to(p / "moved")
    ControlOps._clean_root(str(p), made)
    assert (p / "moved" / "config.json").is_file() and (p / ".orch").is_symlink()
    (p / ".orch").unlink()
    (p / ".orch").mkdir()  # a person's own directory, another inode
    (p / ".orch" / "mine.txt").write_text("keep")
    ControlOps._clean_root(str(p), made)
    assert (p / ".orch" / "mine.txt").read_text() == "keep"


def test_a_failing_bootstrap_leaves_a_pre_existing_dot_orch_untouched(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "proj"
    (p / ".orch").mkdir(parents=True)
    (p / ".orch" / "notes.txt").write_text("another tool's")
    with pytest.raises(StoreError, match="already exists and is not an orch store"):
        Store.open(p, expected_workspace_id="w" * 32, create=True)
    assert (p / ".orch" / "notes.txt").read_text() == "another tool's"
    # what the control layer records for that failed open, and what its rollback then does
    created = ControlOps._fingerprint(str(p), None)
    ControlOps._clean_root(str(p), created)
    assert (p / ".orch" / "notes.txt").read_text() == "another tool's"


def test_store_open_creates_dot_orch_only_with_create_and_records_what_it_made(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    with pytest.raises(StoreError):
        Store.open(p, expected_workspace_id="w" * 32)  # a reader: nothing is made
    assert not (p / ".orch").exists()
    s = Store.open(p, expected_workspace_id="w" * 32, create=True)
    try:
        st = os.stat(p / ".orch")
        assert s.created_store == (st.st_dev, st.st_ino)
        assert stat.S_IMODE(st.st_mode) == 0o700
    finally:
        s.close()


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
    custody.check_workspace_files(tmp_path / "s", "w" * 32, p, os.getuid())
    assert (p / ".orch", True) in seen
    with pytest.raises(HostError, match="owner"):
        custody.check_workspace_files(tmp_path / "s", "w" * 32, p, os.getuid() + 1)


def test_open_store_fs_never_creates_unless_asked(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(OSError):
        open_store_fs(missing, None, None)
    assert not missing.exists()
    with pytest.raises(StoreError):  # the project is made, the store is not (no create)
        open_store_fs(missing, None, None, make_project=True)
    assert missing.is_dir() and not (missing / ".orch").exists()
    with pytest.raises(StoreError):
        open_store_fs(missing, (1, 2), None, create=True)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    with pytest.raises(StoreError):  # no create: no .orch made, the open refuses
        open_store_fs(fresh, None, None)
    assert not (fresh / ".orch").exists()
    fs, lay, made = open_store_fs(fresh, None, None, create=True)
    fs.close()
    assert (fresh / ".orch").is_dir() and lay.store == fresh / ".orch" and made is not None
    shutil.rmtree(fresh / ".orch")


def test_marker_present_is_false_only_for_a_positively_clean_workspace(tmp_path: Path) -> None:
    clean = _proj(tmp_path)
    assert devsandbox.marker_present(clean) is False
    legacy = tmp_path / "old"
    (legacy / ".state").mkdir(parents=True)
    assert devsandbox.marker_present(legacy) is False
    assert devsandbox.marker_present(tmp_path / "does-not-exist") is False  # no root, no marker


def test_a_marker_in_the_old_location_counts_while_the_layout_says_dot_orch(tmp_path: Path) -> None:
    p = _proj(tmp_path)
    (p / ".state").mkdir()
    (p / ".state" / devsandbox.WS_MARKER).write_text("{}")  # hidden from a reader that only trusts the resolved store
    assert devsandbox.marker_present(p) is True
    q = _proj(tmp_path / "q")
    (q / ".orch" / ".state" / devsandbox.WS_MARKER).write_text("{}")
    assert devsandbox.marker_present(q) is True


def test_an_unplaceable_or_unreadable_layout_counts_as_marked(tmp_path: Path) -> None:
    both = _proj(tmp_path / "a")
    (both / "config.json").write_text(CFG)  # two stores
    assert devsandbox.marker_present(both) is True
    linked = tmp_path / "b"
    linked.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (linked / ".orch").symlink_to(tmp_path / "elsewhere")
    assert devsandbox.marker_present(linked) is True
    odd = tmp_path / "c"
    odd.mkdir()
    (odd / ".state").write_text("a file where a directory belongs")
    assert devsandbox.marker_present(odd) is True
    locked = _proj(tmp_path / "d")
    os.chmod(locked / ".orch" / ".state", 0o000)
    try:
        assert devsandbox.marker_present(locked) is True  # cannot show the marker absent
    finally:
        os.chmod(locked / ".orch" / ".state", 0o700)


def test_served_fence_fails_closed_like_the_sandbox_marker(tmp_path: Path) -> None:
    from orch.host.service import SERVED_MARKER, is_served

    clean = _proj(tmp_path)
    assert is_served(clean) is False
    (clean / ".state").mkdir()
    (clean / ".state" / SERVED_MARKER).write_text("{}")  # old place, layout says .orch
    assert is_served(clean) is True
    both = _proj(tmp_path / "b")
    (both / "config.json").write_text(CFG)
    assert is_served(both) is True


def test_the_link_check_for_agents_orch_md_covers_the_dot_orch_component(tmp_path: Path) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (p / ".orch").symlink_to(tmp_path / "elsewhere")
    assert check_targets(p, p / ".orch")
    with pytest.raises(Exception, match="symbolic link"):
        write_workspace_files(p, store=p / ".orch", pointers=False)
    assert not (tmp_path / "elsewhere" / "AGENTS.orch.md").exists()


def test_rollback_empties_the_verified_directory_and_rmdirs_an_empty_tombstone(tmp_path: Path) -> None:
    from orch.store.rootfs import remove_made_dir

    p = _proj(tmp_path)
    (p / ".orch" / "events").mkdir()
    (p / ".orch" / "events" / "x").write_text("1")
    st = os.stat(p / ".orch")
    assert remove_made_dir(p, ".orch", [st.st_dev, st.st_ino]) is True
    assert sorted(os.listdir(p)) == []  # no .orch, no tombstone
    assert remove_made_dir(p, ".orch", [1, 2]) is False


def test_a_failed_open_after_its_own_mkdir_removes_the_dot_orch_it_made(tmp_path: Path, monkeypatch) -> None:
    p = tmp_path / "proj"
    p.mkdir()
    monkeypatch.setattr(Store, "_open", lambda self, *a, **k: (_ for _ in ()).throw(StoreError("validation.path", "x")))
    with pytest.raises(StoreError):
        Store.open(p, expected_workspace_id="w" * 32, create=True)
    assert os.listdir(p) == []  # nothing left behind


def test_read_only_opens_never_make_the_project_directory(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    with pytest.raises(OSError):
        Store.open(missing, expected_workspace_id="w" * 32)
    assert not missing.exists()
