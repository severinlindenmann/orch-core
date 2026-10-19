"""A workspace whose store is still in the project root (an older version) is refused wherever a store is opened,
with one plain message (delete the old files, run orch setup again). There is no migration."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from orch import layout
from orch.cli.main import main
from orch.store import Store, StoreError
from tests.store.helpers import WS


def _world(tmp_path: Path) -> Path:
    """A project holding what an older orch left in the root: its config.json and a few store folders."""
    root = tmp_path / "workspace"
    (root / "events").mkdir(parents=True)
    (root / "events" / "workspace.jsonl").write_text("")
    (root / ".state").mkdir()
    (root / ".state" / "lock").write_text("")
    (root / "config.json").write_text(json.dumps({"schema": "orch.workspace/2", "workspace": {"id": WS}}))
    return root


def test_a_legacy_store_does_not_open_and_says_what_to_delete(tmp_path: Path) -> None:
    root = _world(tmp_path)
    with pytest.raises(StoreError, match="Delete .*run orch setup again"):
        Store.open(root, expected_workspace_id=WS, load="lazy")
    with pytest.raises(StoreError, match="Delete .*run orch setup again"):
        Store.open(root, expected_workspace_id=WS, load="lazy", create=True)
    assert not (root / ".orch").exists()  # nothing is created next to it


def test_resolve_names_the_legacy_store_and_nothing_reads_it(tmp_path: Path) -> None:
    root = _world(tmp_path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(layout.LegacyStoreError, match="Delete config.json, .*run orch setup again"):
            layout.resolve(fd, root)
    finally:
        os.close(fd)
    with pytest.raises(layout.LayoutError):
        layout.store_of(root)
    from orch import discovery

    assert discovery.read_config(root) is None


def test_every_command_answers_with_the_one_fix(tmp_path: Path) -> None:
    root = _world(tmp_path)
    e = {"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(tmp_path / "state"), "HOME": str(tmp_path)}
    out = io.StringIO()
    code = main(["status", "--json"], env=e, stdout=out, stderr=io.StringIO())
    doc = json.loads(out.getvalue())
    assert code != 0 and doc["ok"] is False and "run orch setup again" in json.dumps(doc)


def test_there_is_no_orch_migrate(tmp_path: Path) -> None:
    root = _world(tmp_path)
    e = {"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(tmp_path / "state"), "HOME": str(tmp_path)}
    out = io.StringIO()
    assert main(["migrate", "--json"], env=e, stdout=out, stderr=io.StringIO()) != 0
    assert (root / "config.json").exists() and not (root / ".orch").exists()


def test_only_recognisably_orch_names_are_listed_for_deletion(tmp_path: Path) -> None:
    root = _world(tmp_path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        text = layout.legacy_refusal(fd)
    finally:
        os.close(fd)
    assert text.startswith("this folder has an orch store from an older version in the project root")
    assert "Delete config.json" in text and "run orch setup again" in text
    # a project's own events/ and tickets/ (no orch files in them) are never listed for deletion
    plain = tmp_path / "plain"
    (plain / "events").mkdir(parents=True)
    (plain / "events" / "party.txt").write_text("x")
    (plain / "tickets").mkdir()
    (plain / "tickets" / "a").mkdir()
    (plain / "config.json").write_text("{}")
    fd = os.open(plain, os.O_RDONLY | os.O_DIRECTORY)
    try:
        text = layout.legacy_refusal(fd)
    finally:
        os.close(fd)
    assert text.startswith("this folder has an orch store") and "Delete config.json and run" in text
    assert "check events/, tickets/ before deleting" in text


def test_setup_in_a_legacy_folder_shows_the_delete_message(tmp_path: Path) -> None:
    from orch import rootclaim

    root = _world(tmp_path)
    why = rootclaim.refusal(rootclaim.inspect(root))
    assert why is not None and "Delete" in why and "already an orch workspace" not in why


def test_the_double_store_message_names_only_the_root_config(tmp_path: Path) -> None:
    with pytest.raises(layout.LayoutError, match=r"delete the one in the project root \(config.json\)") as e:
        layout.check_single_store(legacy=True, new=True)
    assert "events" not in str(e.value) and "tickets" not in str(e.value)


def test_a_legacy_config_next_to_a_new_store_is_the_double_store(tmp_path: Path) -> None:
    root = _world(tmp_path)
    (root / ".orch").mkdir()
    (root / ".orch" / "config.json").write_text(json.dumps({"schema": "orch.workspace/2", "workspace": {"id": WS}}))
    with pytest.raises(StoreError, match="two orch stores"):
        Store.open(root, expected_workspace_id=WS, load="lazy")


def test_instructions_sync_in_an_old_root_store_is_refused_and_creates_nothing(tmp_path: Path) -> None:
    root = _world(tmp_path)
    before = sorted(p.name for p in root.iterdir())
    e = {"ORCH_WORKSPACE": str(root), "ORCH_STATE_DIR": str(tmp_path / "state"), "HOME": str(tmp_path)}
    for argv in (["instructions", "sync"], ["instructions", "sync", "--dry-run"], ["check"]):
        out, err = io.StringIO(), io.StringIO()
        code = main(argv, env=e, stdout=out, stderr=err)
        assert code != 0 and "Delete config.json" in out.getvalue() + err.getvalue(), argv
    assert sorted(p.name for p in root.iterdir()) == before and not (root / ".orch").exists()


def test_the_instruction_writer_never_makes_the_store_folder(tmp_path: Path) -> None:
    from orch.instructions import UnsafePath, write_workspace_files

    with pytest.raises(UnsafePath, match="no store folder"):
        write_workspace_files(tmp_path, store=tmp_path / ".orch")
    assert not (tmp_path / ".orch").exists() and sorted(p.name for p in tmp_path.iterdir()) == []
