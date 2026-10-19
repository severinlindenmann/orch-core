"""The shipped default (the flip): ``orch init`` puts the store in ``<project>/.orch`` and nothing else of its own in
the project root but the pointer files; a project's own ``config.json``/``events/``/``tickets/`` are no collision."""

from __future__ import annotations

import json
import stat

import pytest

from orch.ops.errors import OrchError
from orch.store import Store
from tests.instructions.test_init import ARGS, run_init, tree


def test_init_makes_dot_orch_and_only_the_pointer_files_beside_it(where, term, passphrases):
    res = run_init(where)
    root = where["root"]
    assert res.data["prefix"] == "DEMO"
    top = {p.name for p in root.iterdir()}
    assert top == {".orch", ".agents", ".claude", "AGENTS.md", "CLAUDE.md", ".gitignore"}
    store = root / ".orch"
    assert stat.S_IMODE(store.stat().st_mode) == 0o700 and not store.is_symlink()
    cfg = json.loads((store / "config.json").read_text())
    assert cfg["schema"] == "orch.workspace/2"
    for name in ("keys.jsonl", "AGENTS.orch.md", "events", "tickets", ".state"):
        assert (store / name).exists(), name
    assert (store / "events" / "workspace.jsonl").read_bytes().count(b"\n") == 1
    # the pointers: AGENTS.md names the file in .orch, CLAUDE.md imports AGENTS.md only, .orch/ is ignored whole
    assert ".orch/AGENTS.orch.md" in (root / "AGENTS.md").read_text()
    assert (root / "CLAUDE.md").read_text().split("\n")[:2] == ["@AGENTS.md", ""]
    assert "@AGENTS.orch.md" not in (root / "CLAUDE.md").read_text()
    assert ".orch/" in (root / ".gitignore").read_text().split("\n")
    assert ".state/" not in (root / ".gitignore").read_text().split("\n")
    assert (root / ".agents/skills/orch-tickets/SKILL.md").is_file()
    s = Store.open(root, expected_workspace_id=cfg["workspace"]["id"], load="all")
    try:
        assert not s.layout.legacy and s.state is not None
    finally:
        s.close()


def test_a_project_with_its_own_config_events_and_tickets_folders_can_init(where, term, passphrases):
    root = where["root"]
    (root / "config.json").write_text('{"name": "my js project"}')
    (root / "events").mkdir()
    (root / "events" / "a.txt").write_text("mine")
    (root / "tickets").mkdir()
    res = run_init(where)
    assert res.data["prefix"] == "DEMO"
    assert (root / "config.json").read_text() == '{"name": "my js project"}'
    assert (root / "events" / "a.txt").read_text() == "mine" and (root / "tickets").is_dir()
    assert (root / ".orch" / "config.json").is_file()


def test_a_projects_own_dot_orch_is_refused_and_left_untouched(where, term, passphrases):
    root = where["root"]
    (root / ".orch").mkdir()
    (root / ".orch" / "notes.txt").write_text("another tool's")
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert e.value.code == "invalid.input" and "own .orch/" in e.value.message
    assert (root / ".orch" / "notes.txt").read_text() == "another tool's" and not where["state"].exists()


def test_a_legacy_root_store_and_a_store_move_in_progress_are_refused(where, term, passphrases):
    root = where["root"]
    (root / "config.json").write_text(json.dumps({"schema": "orch.workspace/2", "workspace": {"id": "w"}}))
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert "workspace already exists" in e.value.message
    (root / "config.json").unlink()
    (root / ".orch-migrating").mkdir()
    with pytest.raises(OrchError) as e:
        run_init(where)
    assert "run orch setup again" in e.value.message


@pytest.mark.parametrize("at", ["Passphrase: ", "Press Enter"])
def test_a_cancelled_init_removes_the_dot_orch_it_made_and_nothing_else(where, term, passphrases, at):
    (where["root"] / "README.md").write_text("mine")
    term.interrupt_at = at
    with pytest.raises(OrchError):
        run_init(where)
    assert tree(where["root"]) == {"README.md"}
    assert ARGS


def test_sync_upgrades_the_pointers_of_a_migrated_workspace(tmp_path):
    from orch.instructions import write_workspace_files

    root = tmp_path / "p"
    (root / ".orch").mkdir(parents=True)
    (root / "AGENTS.md").write_text(
        "# mine\n\n<!-- orch:begin -->\nThis project uses orch for tickets: read AGENTS.orch.md before working on a "
        "ticket.\n<!-- orch:end -->\n"
    )
    (root / "CLAUDE.md").write_text("# rules\n@AGENTS.md\n@AGENTS.orch.md\n")
    changed, _ = write_workspace_files(root, pointers=False, store=root / ".orch")
    assert ".orch/AGENTS.orch.md" in changed and (root / ".orch" / "AGENTS.orch.md").is_file()
    assert (
        "read .orch/AGENTS.orch.md" in (root / "AGENTS.md").read_text() and "# mine" in (root / "AGENTS.md").read_text()
    )
    assert (root / "CLAUDE.md").read_text() == "# rules\n@AGENTS.md\n"
    again, _ = write_workspace_files(root, pointers=False, store=root / ".orch")
    assert again == []
