"""``orch.rootclaim`` with the shipped default (the flip): the store takes ``.orch`` and nothing else in the project
root; an old-version root store, an init in progress and a project's own ``.orch`` are the only refusals."""

from __future__ import annotations

import json
from pathlib import Path

from orch import rootclaim

CFG = json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}})


def _free(root: Path) -> str | None:
    return rootclaim.refusal(rootclaim.inspect(root), init_lock_ok=True)


def test_a_projects_generic_names_are_no_collision(tmp_path: Path) -> None:
    for name in ("config.json", "keys.jsonl", "AGENTS.orch.md"):
        (tmp_path / name).write_text("{}")
    for name in ("events", "tickets", ".state", "addons"):
        (tmp_path / name).mkdir()
    assert rootclaim.WRITES == (".orch",)
    assert _free(tmp_path) is None


def test_a_root_store_from_an_older_version_says_to_delete_it(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(CFG)
    why = _free(tmp_path) or ""
    assert "Delete config.json" in why and "run orch setup again" in why and "already an orch workspace" not in why


def test_an_orch_store_in_dot_orch_is_a_workspace(tmp_path: Path) -> None:
    (tmp_path / ".orch").mkdir()
    (tmp_path / ".orch" / "config.json").write_text(CFG)
    assert "already an orch workspace" in (_free(tmp_path) or "")


def test_a_projects_own_dot_orch_is_a_named_collision_not_orch(tmp_path: Path) -> None:
    (tmp_path / ".orch").mkdir()
    (tmp_path / ".orch" / "config.json").write_text('{"name": "another tool"}')
    view = rootclaim.inspect(tmp_path)
    assert not view.orch and view.collisions == (".orch",)
    assert "own .orch/" in (rootclaim.refusal(view) or "")
    link = tmp_path / "p"
    link.mkdir()
    (link / ".orch").symlink_to(tmp_path)
    assert not rootclaim.inspect(link).orch and "own .orch/" in (rootclaim.refusal(rootclaim.inspect(link)) or "")


def test_an_init_in_progress_is_a_named_marker(tmp_path: Path) -> None:
    (tmp_path / ".orch-migrating").mkdir()  # nothing of orch's any more: a project's own name
    assert _free(tmp_path) is None
    (tmp_path / ".orch-init.lock").write_text("")
    assert _free(tmp_path) is None  # init's own lock is fine for init
    assert "orch init" in (rootclaim.refusal(rootclaim.inspect(tmp_path)) or "")
