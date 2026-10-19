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
