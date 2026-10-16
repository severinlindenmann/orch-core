"""The harness files: the plugin layout in the repository, the workspace files, the AGENTS.md / CLAUDE.md pointers,
and the skills as package data of the wheel."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from orch.instructions import UnsafePath, claude_plugin_enabled, plugin_files, workspace_files, write_workspace_files
from orch.instructions.harness import AGENTS_BLOCK, CLAUDE_IMPORT, COPY_HEADER
from orch.instructions.skills import SKILL_NAMES

PLUGIN = Path(__file__).resolve().parents[2]  # plugins/orch-core


def test_the_plugin_layout_in_the_repository_is_what_the_generator_writes():
    """``scripts/sync-plugin.sh`` regenerates ``skills/`` and ``hooks/hooks.json``; a hand edit fails."""
    for rel, text in plugin_files().items():
        assert (PLUGIN / rel).read_text() == text, f"{rel} is stale: run plugins/orch-core/scripts/sync-plugin.sh"
    on_disk = {p.relative_to(PLUGIN).as_posix() for p in (PLUGIN / "skills").rglob("*") if p.is_file()}
    on_disk |= {p.relative_to(PLUGIN).as_posix() for p in (PLUGIN / "hooks").rglob("*") if p.is_file()}
    assert on_disk == set(plugin_files())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "orch-core"


def skill_files(base):
    return {f"{base}/{n}/{f}" for n in SKILL_NAMES for f in ("SKILL.md", "orch.skill.json")}


def test_workspace_files_are_the_instructions_the_skills_and_the_generated_claude_copy():
    files = workspace_files()
    assert set(files) == {"AGENTS.orch.md"} | skill_files(".agents/skills") | skill_files(".claude/skills")
    assert set(workspace_files(claude_copy=False)) == {"AGENTS.orch.md"} | skill_files(".agents/skills")
    for n in SKILL_NAMES:  # the copy is the same skill with one header line after the frontmatter
        canon, copy = files[f".agents/skills/{n}/SKILL.md"], files[f".claude/skills/{n}/SKILL.md"]
        assert COPY_HEADER not in canon and copy.replace(COPY_HEADER + "\n", "", 1) == canon
        assert copy.split("\n")[copy.split("\n").index("---", 1) + 1] == COPY_HEADER
        assert files[f".agents/skills/{n}/orch.skill.json"] == files[f".claude/skills/{n}/orch.skill.json"]


def test_writing_is_idempotent_and_repairs_a_damaged_file(tmp_path):
    first, kept = write_workspace_files(tmp_path)
    assert {"AGENTS.orch.md", "AGENTS.md", "CLAUDE.md", ".gitignore"} <= set(first) and kept == []
    assert skill_files(".agents/skills") <= set(first) and skill_files(".claude/skills") <= set(first)
    assert write_workspace_files(tmp_path) == ([], [])  # nothing changes the second time
    (tmp_path / "AGENTS.orch.md").write_text("tampered\n")
    assert write_workspace_files(tmp_path, pointers=False) == (["AGENTS.orch.md"], [])
    assert (tmp_path / "AGENTS.md").read_text() == AGENTS_BLOCK + "\n"
    assert (tmp_path / "CLAUDE.md").read_text() == CLAUDE_IMPORT + "\n" == "@AGENTS.md\n"


def test_the_agents_md_block_is_appended_once_and_replaced_in_place(tmp_path):
    (tmp_path / "AGENTS.md").write_text("keep this\nand this")  # no trailing newline
    write_workspace_files(tmp_path)
    write_workspace_files(tmp_path)
    text = f"keep this\nand this\n\n{AGENTS_BLOCK}\n"
    assert (tmp_path / "AGENTS.md").read_text() == text
    # an edited block is repaired in place, text before and after stays where it is
    edited = text.replace("read AGENTS.orch.md", "read nothing").replace("and this\n", "and this\n") + "after\n"
    (tmp_path / "AGENTS.md").write_text(edited)
    assert write_workspace_files(tmp_path, pointers=False)[0] == ["AGENTS.md"]
    assert (tmp_path / "AGENTS.md").read_text() == text + "after\n"
    assert write_workspace_files(tmp_path, pointers=False) == ([], [])


def test_the_old_one_line_pointer_becomes_the_block_and_sync_does_it_too(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\nBefore working on tickets, read AGENTS.orch.md (orch).\nmore\n")
    changed, _ = write_workspace_files(tmp_path, pointers=False)  # what `orch instructions sync` does
    assert "AGENTS.md" in changed
    assert (tmp_path / "AGENTS.md").read_text() == f"# Rules\n{AGENTS_BLOCK}\nmore\n"


def test_sync_alone_creates_no_agents_md_or_claude_md(tmp_path):
    changed, _ = write_workspace_files(tmp_path, pointers=False)
    assert "AGENTS.md" not in changed and "CLAUDE.md" not in changed and not (tmp_path / "AGENTS.md").exists()


def test_unbalanced_block_markers_are_refused_not_guessed(tmp_path):
    (tmp_path / "AGENTS.md").write_text("<!-- orch:begin -->\nmy text\n")
    with pytest.raises(ValueError, match="markers"):
        write_workspace_files(tmp_path, pointers=False)
    assert (tmp_path / "AGENTS.md").read_text() == "<!-- orch:begin -->\nmy text\n"


def test_claude_md_imports_agents_md_and_the_old_import_is_replaced(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# mine\n@AGENTS.orch.md\n@other.md\n")
    changed, _ = write_workspace_files(tmp_path, pointers=False)
    assert "CLAUDE.md" in changed and "AGENTS.md" in changed  # AGENTS.md gets its block so the import resolves
    assert (tmp_path / "CLAUDE.md").read_text() == "# mine\n@AGENTS.md\n@other.md\n"
    assert write_workspace_files(tmp_path, pointers=False) == ([], [])
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n@AGENTS.orch.md\n")  # both: the old one is dropped
    write_workspace_files(tmp_path, pointers=False)
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    (tmp_path / "CLAUDE.md").write_text("only mine")  # user content without any import is appended to
    write_workspace_files(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == "only mine\n@AGENTS.md\n"


def test_plugin_detection_reads_the_project_settings(tmp_path):
    assert not claude_plugin_enabled(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text('{"enabledPlugins": {"orch-core@orch": true}}')
    assert claude_plugin_enabled(tmp_path)
    (tmp_path / ".claude/settings.json").write_text('{"enabledPlugins": {"orch-core@orch": false}}')
    assert not claude_plugin_enabled(tmp_path)
    (tmp_path / ".claude/settings.json").write_text("not json")
    assert not claude_plugin_enabled(tmp_path)


def test_with_the_plugin_enabled_no_claude_copy_is_written_and_an_old_one_moves(tmp_path):
    write_workspace_files(tmp_path)  # a copy exists (plugin not enabled)
    (tmp_path / ".claude/settings.json").write_text('{"enabledPlugins": {"orch-core@orch": true}}')
    mine = tmp_path / ".claude/skills/orch-tickets/SKILL.md"
    extra = tmp_path / ".claude/skills/orch-refine-ticket/notes.txt"
    extra.write_text("mine")
    mine.write_text(mine.read_text() + "my rule\n")  # hand-edited: stays
    changed, kept = write_workspace_files(tmp_path, pointers=False)
    assert changed == []
    assert any(x.startswith("kept .claude/skills/orch-tickets (edited by hand") for x in kept)
    assert "moved .claude/skills/orch-work-on-ticket to .agents/skills/orch-work-on-ticket" in kept
    assert not (tmp_path / ".claude/skills/orch-work-on-ticket").exists()
    assert extra.read_text() == "mine"  # a directory with other files is not removed
    assert (tmp_path / ".agents/skills/orch-work-on-ticket/SKILL.md").is_file()


def test_legacy_claude_skills_without_the_header_are_orch_built_not_edited(tmp_path):
    write_workspace_files(tmp_path)
    for n in SKILL_NAMES:  # what an older orch wrote: the plain text
        p = tmp_path / f".claude/skills/{n}/SKILL.md"
        p.write_text(p.read_text().replace(COPY_HEADER + "\n", ""))
    changed, kept = write_workspace_files(tmp_path, pointers=False)
    assert kept == [] and sorted(changed) == sorted(f".claude/skills/{n}/SKILL.md" for n in SKILL_NAMES)


def test_a_user_skill_next_to_the_orch_ones_is_never_touched(tmp_path):
    (tmp_path / ".agents/skills/mine").mkdir(parents=True)
    (tmp_path / ".agents/skills/mine/SKILL.md").write_text("mine\n")
    (tmp_path / ".claude/skills/theirs").mkdir(parents=True)
    (tmp_path / ".claude/skills/theirs/SKILL.md").write_text("theirs\n")
    (tmp_path / ".claude/settings.json").write_text('{"enabledPlugins": {"orch-core@orch": true}}')
    write_workspace_files(tmp_path)
    assert (tmp_path / ".agents/skills/mine/SKILL.md").read_text() == "mine\n"
    assert (tmp_path / ".claude/skills/theirs/SKILL.md").read_text() == "theirs\n"


def test_a_symlinked_skills_folder_is_refused(tmp_path):
    write_workspace_files(tmp_path)
    (tmp_path / ".claude/settings.json").write_text('{"enabledPlugins": {"orch-core@orch": true}}')
    other = tmp_path.parent / (tmp_path.name + "-elsewhere")
    other.mkdir()
    (other / "orch-tickets").mkdir()
    (other / "orch-tickets/SKILL.md").write_text("secret\n")
    shutil.rmtree(tmp_path / ".claude/skills")
    (tmp_path / ".claude/skills").symlink_to(other)
    with pytest.raises(UnsafePath):
        write_workspace_files(tmp_path)
    assert (other / "orch-tickets/SKILL.md").read_text() == "secret\n"
    shutil.rmtree(tmp_path / ".agents/skills")
    (tmp_path / ".agents/skills").symlink_to(other)
    with pytest.raises(UnsafePath):
        write_workspace_files(tmp_path)


@pytest.mark.slow
def test_the_wheel_ships_the_skills_and_the_sidecars(tmp_path):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed")
    out = tmp_path / "dist"
    subprocess.run([uv, "build", "--wheel", "--out-dir", str(out), str(PLUGIN)], check=True, capture_output=True)
    (wheel,) = out.glob("orch_core-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for n in SKILL_NAMES:
        assert f"orch/instructions/skills/{n}/SKILL.md" in names
        assert f"orch/instructions/skills/{n}/orch.skill.json" in names
    assert "orch/instructions/agents_md.py" in names
    assert "orch/custody/common_passwords.txt" in names
