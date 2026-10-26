"""The harness files: the plugin layout in the repository, the workspace files, the AGENTS.md / CLAUDE.md pointers,
and the skills as package data of the wheel."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from orch.instructions import UnsafePath, check_targets, plugin_files, workspace_files, write_workspace_files
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
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n@AGENTS.orch.md\n" and CLAUDE_IMPORT == "@AGENTS.md"


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
    with pytest.raises(UnsafePath, match="markers"):
        write_workspace_files(tmp_path, pointers=False)
    assert (tmp_path / "AGENTS.md").read_text() == "<!-- orch:begin -->\nmy text\n"


def test_claude_md_gets_both_imports_and_an_older_file_is_completed(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# mine\n@AGENTS.orch.md\n@other.md\n")  # what r1 wrote (plus user lines)
    changed, _ = write_workspace_files(tmp_path, pointers=False)
    assert "CLAUDE.md" in changed and "AGENTS.md" in changed  # AGENTS.md gets its block so the import resolves
    assert (tmp_path / "CLAUDE.md").read_text() == "# mine\n@AGENTS.md\n@AGENTS.orch.md\n@other.md\n"
    assert write_workspace_files(tmp_path, pointers=False) == ([], [])
    (tmp_path / "CLAUDE.md").write_text("only mine")  # init appends both to user content
    write_workspace_files(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == "only mine\n@AGENTS.md\n@AGENTS.orch.md\n"
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\nmine\n")  # one line there: init adds the other, sync does not
    assert write_workspace_files(tmp_path, pointers=False)[0] == []
    write_workspace_files(tmp_path)
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\nmine\n@AGENTS.orch.md\n"


def test_the_claude_copy_is_always_written_and_never_deleted(tmp_path):
    write_workspace_files(tmp_path)
    (tmp_path / ".claude/settings.json").write_text('{"enabledPlugins": {"orch-core@orch": true}}')
    changed, kept = write_workspace_files(tmp_path, pointers=False)
    assert changed == [] and kept == []
    assert skill_files(".claude/skills") <= {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")}
    for n in SKILL_NAMES:
        (tmp_path / f".claude/skills/{n}/SKILL.md").unlink()
    changed, _ = write_workspace_files(tmp_path, pointers=False)
    assert sorted(changed) == sorted(f".claude/skills/{n}/SKILL.md" for n in SKILL_NAMES)


def test_an_edited_claude_copy_is_kept_and_the_canonical_one_still_written(tmp_path):
    write_workspace_files(tmp_path)
    mine = tmp_path / ".claude/skills/orch-tickets/SKILL.md"
    mine.write_text(mine.read_text() + "my rule\n")
    shutil.rmtree(tmp_path / ".agents")
    changed, kept = write_workspace_files(tmp_path, pointers=False)
    assert any(x.startswith("kept .claude/skills/orch-tickets (edited by hand") for x in kept)
    assert ".agents/skills/orch-tickets/SKILL.md" in changed and "my rule" in mine.read_text()


def test_a_skill_folder_without_a_sidecar_is_the_users_unless_it_is_the_shipped_text(tmp_path):
    for base in (".agents/skills", ".claude/skills"):
        d = tmp_path / base / "orch-tickets"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("my own skill with an orch name\n")
    changed, kept = write_workspace_files(tmp_path)
    assert "kept .agents/skills/orch-tickets (no sidecar, not orch's)" in kept
    assert "kept .claude/skills/orch-tickets (no sidecar, not orch's)" in kept
    assert (tmp_path / ".agents/skills/orch-tickets/SKILL.md").read_text() == "my own skill with an orch name\n"
    assert not (tmp_path / ".agents/skills/orch-tickets/orch.skill.json").exists()
    assert ".agents/skills/orch-work-on-ticket/SKILL.md" in changed
    # --force does not make a foreign skill orch's
    write_workspace_files(tmp_path, force=True)
    assert (tmp_path / ".claude/skills/orch-tickets/SKILL.md").read_text() == "my own skill with an orch name\n"
    # exactly the shipped text without a sidecar is orch's: the sidecar is added
    shipped = workspace_files()[".agents/skills/orch-tickets/SKILL.md"]
    (tmp_path / ".agents/skills/orch-tickets/SKILL.md").write_text(shipped)
    changed, kept = write_workspace_files(tmp_path)
    assert ".agents/skills/orch-tickets/orch.skill.json" in changed
    assert not any(".agents/skills/orch-tickets" in x for x in kept)


def test_unsafe_pointer_files_fail_before_anything_is_written(tmp_path):
    (tmp_path / "AGENTS.md").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(UnsafePath, match="UTF-8"):
        write_workspace_files(tmp_path)
    (tmp_path / "AGENTS.md").write_text("fine\n")
    (tmp_path / "CLAUDE.md").write_bytes(b"x" * ((1 << 20) + 1))
    with pytest.raises(UnsafePath, match="larger"):
        write_workspace_files(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    elsewhere = tmp_path.parent / (tmp_path.name + "-other.md")
    elsewhere.write_text("x\n")
    (tmp_path / "CLAUDE.md").symlink_to(elsewhere)
    with pytest.raises(UnsafePath):
        write_workspace_files(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["AGENTS.md", "CLAUDE.md"]  # nothing else was written
    assert (tmp_path / "AGENTS.md").read_text() == "fine\n"


def test_a_claude_md_linked_to_agents_md_is_skipped_not_fatal(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# rules\n")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    assert check_targets(tmp_path) == []
    changed, kept = write_workspace_files(tmp_path)
    assert "CLAUDE.md" not in changed and "AGENTS.md" in changed
    assert any(x.startswith("kept CLAUDE.md (a link to AGENTS.md") for x in kept)
    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "AGENTS.md").read_text().startswith("# rules\n\n<!-- orch:begin -->")
    assert write_workspace_files(tmp_path)[0] == []


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
    write_workspace_files(tmp_path)
    assert (tmp_path / ".agents/skills/mine/SKILL.md").read_text() == "mine\n"
    assert (tmp_path / ".claude/skills/theirs/SKILL.md").read_text() == "theirs\n"


def test_a_symlinked_skills_folder_is_refused(tmp_path):
    write_workspace_files(tmp_path)
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
