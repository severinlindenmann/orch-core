"""The three built-in skills: portable SKILL.md, a valid D55 sidecar, judgment rules only, no unknown commands."""

from __future__ import annotations

import json

import pytest

from orch.instructions import SKILL_MAX_CHARS, Skill, builtin_skills, check_sidecar, parse_frontmatter
from orch.instructions.skills import SKILL_NAMES, SkillError
from tests.instructions.helpers import backticked, command_references, unknown_references

SKILLS = builtin_skills()


def test_there_are_exactly_the_three_skills_of_the_spec():
    assert (
        [s.name for s in SKILLS] == ["orch-tickets", "orch-work-on-ticket", "orch-refine-ticket"] == list(SKILL_NAMES)
    )


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_skill_md_is_the_portable_format(skill: Skill):
    meta, body = parse_frontmatter(skill.text)
    assert set(meta) == {"name", "description"} and meta["name"] == skill.name  # nothing else in the frontmatter
    assert meta["description"].startswith("Use when") and len(meta["description"]) <= 200
    assert body.lstrip().startswith("# ") and skill.text.endswith("\n") and "\r" not in skill.text


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_sidecar_is_valid_d55_and_builtin(skill: Skill):
    assert check_sidecar(skill.sidecar) == skill.sidecar
    assert skill.sidecar == {
        "schema_version": 1,
        "skill_version": skill.version,
        "scope": "builtin",
        "connections": [],  # a built-in skill needs no credential
        "env": [],
    }
    assert json.loads(skill.files()[f"{skill.name}/orch.skill.json"]) == skill.sidecar


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "builtin", "connections": []},  # no env
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "builtin", "connections": [], "env": [], "x": 1},
        {"schema_version": 2, "skill_version": "1.0.0", "scope": "builtin", "connections": [], "env": []},
        {"schema_version": True, "skill_version": "1.0.0", "scope": "builtin", "connections": [], "env": []},
        {"schema_version": 1, "skill_version": "1.0", "scope": "builtin", "connections": [], "env": []},
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "global", "connections": [], "env": []},
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "org", "connections": "db", "env": []},
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "org", "connections": ["a", "a"], "env": []},
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "org", "connections": [], "env": ["lower"]},
        {"schema_version": 1, "skill_version": "1.0.0", "scope": "org", "connections": [], "env": ["A B"]},
    ],
)
def test_a_bad_sidecar_is_refused(bad):
    with pytest.raises(SkillError):
        check_sidecar(bad)


def test_a_sidecar_with_a_connection_and_an_env_name_is_valid():
    doc = {
        "schema_version": 1,
        "skill_version": "1.2.0",
        "scope": "workspace",
        "connections": ["databricks-prod"],
        "env": ["DATABRICKS_HOST"],
    }
    assert check_sidecar(doc) == doc


@pytest.mark.parametrize(
    "text",
    [
        "no frontmatter\n",
        "---\nname: a\n",  # not closed
        "---\nname: a\n---\n",  # no description
        "---\nname: a\ndescription: d\nlicense: x\n---\n",  # a third key
        "---\nname: A_b\ndescription: d\n---\n",
        "---\nname: a\nname: b\ndescription: d\n---\n",
    ],
)
def test_bad_frontmatter_is_refused(text):
    with pytest.raises(SkillError):
        parse_frontmatter(text)


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_no_command_that_is_not_in_the_registry(skill: Skill):
    assert unknown_references(backticked(skill.text)) == []


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_judgment_rules_only_no_command_manual(skill: Skill):
    _meta, body = parse_frontmatter(skill.text)
    assert "```" not in skill.text  # no code blocks: a skill is not a manual
    lines = [x for x in body.splitlines() if x.strip()]
    assert not [x for x in lines if x.startswith(("orch ", "$ ", "    "))]
    refs = command_references(backticked(body))
    assert len(refs) <= 4, [m for m, _o, _f in refs]  # it points at commands, it does not teach them
    assert not [m for m, _o, flags in refs if flags], "no flags in a skill: orch describe has them"
    bullets = [x for x in lines if x.startswith("- ")]
    assert len(bullets) >= 6 and len(bullets) >= len(lines) - 4  # a few lines of intro, the rest are rules
    assert "orch describe" in body or "orch help" in body  # where the commands are


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_each_skill_is_small(skill: Skill):
    assert len(skill.text) <= SKILL_MAX_CHARS and len(skill.text.splitlines()) <= 30
