"""Renderers run against the real checkout: the files an agent finds under each condition."""
import pytest

import addons as A
import renderers as R


def test_today_writes_no_addon_line():
    files, info = R.render("today", A.ORDER[12], A.OFF)
    text = files["AGENTS.orch.md"]
    assert "addon" not in text.lower() and not info["over_cap"]
    from orch.instructions.agents_md import render_agents_md
    assert text == render_agents_md()
    assert not [k for k in files if "orch-addon-" in k]
    assert files["CLAUDE.md"].splitlines() == ["@AGENTS.md", "@AGENTS.orch.md"]
    assert any(k.startswith(".claude/skills/orch-tickets/") for k in files)  # the real built-in skills


def test_oneliners_use_the_real_render_until_the_cap():
    files, info = R.render("today-oneliners", A.ORDER[5], A.OFF)
    assert "- addon quick (hint, grants nothing): Quick tasks:" in files["AGENTS.orch.md"]
    assert info["agents_lines"] <= 40 and not info["over_cap"]
    files12, info12 = R.render("today-oneliners", A.ORDER[12], A.OFF)
    assert info12["over_cap"] and info12["agents_lines"] > 40  # the real renderer refuses; the eval shows the lines anyway
    assert files12["AGENTS.orch.md"].count("- addon ") == len([a for a in A.ORDER[12] if A.TODAY.get(a)])


def test_proposed_block_and_skills():
    files, info = R.render("proposed", A.ORDER[12], A.OFF)
    text = files["AGENTS.orch.md"]
    assert "## Add-ons here" in text and "Off here: time-tracking (disabled)." in text
    block = R.proposed_block(A.ORDER[12], A.OFF)
    assert len(block.splitlines()) <= R.BLOCK_MAX_LINES and len(block) <= R.BLOCK_MAX_CHARS
    names = [l.split(":")[0][2:] for l in block.splitlines() if l.startswith("- ")]
    assert names == sorted(names) and len(names) == 12
    for a in A.ORDER[12]:
        assert files[f".claude/skills/orch-addon-{a}/SKILL.md"].startswith(f"---\nname: orch-addon-{a}\n")
    assert text.startswith(R.render("today", [], [])[0]["AGENTS.orch.md"].rstrip("\n"))  # core text unchanged


def test_proposed_block_with_nothing_off_and_over_limit():
    assert R.proposed_block(["quick"], []).splitlines()[-1] == "Off here: none."
    # 13 agent-facing add-ons: render fails rather than truncating
    extra = {f"a{i}": f"a{i}: use for thing {i}. Skill: orch-addon-a{i}." for i in range(5)}
    A.INDEX.update(extra)
    try:
        with pytest.raises(ValueError, match="at most 12"):
            R.proposed_block(A.ORDER[12] + list(extra), [])
    finally:
        for k in extra:
            del A.INDEX[k]


def test_unknown_renderer():
    with pytest.raises(ValueError):
        R.render("nope", [], [])


def test_skills_fit_the_budget():
    from orch.instructions.skills import SKILL_MAX_CHARS
    for a in A.ORDER[12]:
        assert len(R.skill_text(a, A.SKILL_V2)) <= SKILL_MAX_CHARS
