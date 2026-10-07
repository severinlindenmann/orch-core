import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from orch.core.model import yaml_load
from orch.instructions import sync

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_skill_names_and_frontmatter():
    assert sync.SKILL_NAMES == ("orch-tickets", "orch-refine-ticket", "orch-work-on-ticket", "orch-setup")
    for name in sync.SKILL_NAMES:
        text = (sync.skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
        meta = yaml_load(text.split("---\n")[1])
        assert meta["name"] == name


def test_skills_dir_falls_back_to_plugin_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "_PACKAGED_SKILLS", tmp_path / "missing")
    assert sync.skills_dir() == PLUGIN_ROOT / "skills"


def test_no_old_skill_names_left():
    offenders = []
    for path in list((PLUGIN_ROOT / "src").rglob("*.py")) + list((PLUGIN_ROOT / "skills").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "the tickets skill" in text or "`tickets` (commands" in text:
            offenders.append(str(path))
    assert offenders == []


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_wheel_contains_skills(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=PLUGIN_ROOT, check=True,
                   capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for name in sync.SKILL_NAMES:
        assert f"orch/instructions/skills/{name}/SKILL.md" in names
    assert "orch/schemas/tasks-view.schema.json" in names  # orch schema ticket embeds it


def test_skills_teach_the_task_list():
    root = sync.skills_dir()
    work = (root / "orch-work-on-ticket" / "SKILL.md").read_text(encoding="utf-8")
    for phrase in ("orch task list <id> --json", "orch task add <id> --file", "orch task start", "orch task done",
                   "every task done or skipped", "owner: human",
                   "Every size needs at least one task", "means exactly this", "orch wait <id> --json"):
        assert phrase in work, phrase
    tickets = (root / "orch-tickets" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Tasks file" in tickets and "orch task block" in tickets and "taken over by you" in tickets
    assert "orch wait <id> [--timeout S] --json" in tickets
    for kind in ("file:", "static:", "artifact:", "ticket:", "ext:", "url:", "section:", "ac:", "q:"):
        assert kind in tickets, kind
    refine = (root / "orch-refine-ticket" / "SKILL.md").read_text(encoding="utf-8")
    assert "Do not create tasks" in refine and "`ac:1`" in refine
