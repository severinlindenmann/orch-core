"""The harness files: the plugin layout in the repository, the workspace files, the AGENTS.md / CLAUDE.md pointers,
and the skills as package data of the wheel."""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from orch.instructions import plugin_files, workspace_files, write_workspace_files
from orch.instructions.harness import AGENTS_POINTER, CLAUDE_IMPORT
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


def test_workspace_files_are_the_instructions_and_the_three_skills():
    files = workspace_files()
    assert set(files) == {"AGENTS.orch.md"} | {
        f".claude/skills/{n}/{f}" for n in SKILL_NAMES for f in ("SKILL.md", "orch.skill.json")
    }


def test_writing_is_idempotent_and_repairs_a_damaged_file(tmp_path):
    first, kept = write_workspace_files(tmp_path)
    assert {"AGENTS.orch.md", "AGENTS.md", "CLAUDE.md", ".gitignore"} <= set(first) and kept == []
    assert write_workspace_files(tmp_path) == ([], [])  # nothing changes the second time
    (tmp_path / "AGENTS.orch.md").write_text("tampered\n")
    assert write_workspace_files(tmp_path, pointers=False) == (["AGENTS.orch.md"], [])
    assert (tmp_path / "AGENTS.md").read_text() == AGENTS_POINTER + "\n"
    assert (tmp_path / "CLAUDE.md").read_text() == CLAUDE_IMPORT + "\n"


def test_pointer_lines_are_appended_once_and_never_rewrite_the_rest(tmp_path):
    (tmp_path / "AGENTS.md").write_text("keep this\nand this")  # no trailing newline
    write_workspace_files(tmp_path)
    write_workspace_files(tmp_path)
    assert (tmp_path / "AGENTS.md").read_text() == f"keep this\nand this\n{AGENTS_POINTER}\n"


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
