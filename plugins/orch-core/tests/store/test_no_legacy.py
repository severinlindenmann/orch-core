"""The final state of the move: a workspace whose store is still in the project root is refused wherever a store is
opened, with one plain message (delete the old files, run orch setup again)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from orch import layout
from orch.cli.main import main
from orch.store import Store, StoreError
from tests.store.helpers import WS
from tests.store.test_migrate import _world as _legacy_world

pytestmark = pytest.mark.no_legacy


def _world(tmp_path: Path):
    """A legacy workspace, built while legacy stores are still openable (as one made before the move)."""
    layout.LEGACY_ALLOWED = True
    try:
        return _legacy_world(tmp_path)
    finally:
        layout.LEGACY_ALLOWED = False


def test_a_legacy_store_does_not_open_and_says_how_to_move_it(tmp_path: Path) -> None:
    env = _world(tmp_path)
    with pytest.raises(StoreError, match="Delete .*run orch setup again"):
        Store.open(env.root, expected_workspace_id=WS, load="lazy")


def test_every_command_answers_with_the_one_fix(tmp_path: Path) -> None:
    env = _world(tmp_path)
    e = {"ORCH_WORKSPACE": str(env.root), "ORCH_STATE_DIR": str(env.host_state), "HOME": str(env.tmp)}
    out = io.StringIO()
    code = main(["status", "--json"], env=e, stdout=out, stderr=io.StringIO())
    doc = json.loads(out.getvalue())
    assert code != 0 and doc["ok"] is False and "run orch setup again" in json.dumps(doc)


def test_orch_migrate_still_works_and_the_moved_workspace_opens(tmp_path: Path) -> None:
    env = _world(tmp_path)
    e = {"ORCH_WORKSPACE": str(env.root), "ORCH_STATE_DIR": str(env.host_state), "HOME": str(env.tmp)}
    out = io.StringIO()
    assert main(["migrate", "--json"], env=e, stdout=out, stderr=io.StringIO()) == 0, out.getvalue()
    s = Store.open(env.root, expected_workspace_id=WS, load="all")
    try:
        assert not s.layout.legacy
    finally:
        s.close()
    out = io.StringIO()
    assert main(["status", "--json"], env=e, stdout=out, stderr=io.StringIO()) == 0, out.getvalue()


def test_only_recognisably_orch_names_are_listed_for_deletion(tmp_path: Path) -> None:
    import os

    env = _world(tmp_path)
    fd = os.open(env.root, os.O_RDONLY | os.O_DIRECTORY)
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

    env = _world(tmp_path)
    why = rootclaim.refusal(rootclaim.inspect(env.root))
    assert why is not None and "Delete" in why and "already an orch workspace" not in why


def test_the_double_store_message_names_only_the_root_config(tmp_path: Path) -> None:
    with pytest.raises(layout.LayoutError, match=r"delete the one in the project root \(config.json\)") as e:
        layout.check_single_store(legacy=True, new=True)
    assert "events" not in str(e.value) and "tickets" not in str(e.value)
