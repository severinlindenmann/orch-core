"""F3: the agent's guidance makes the human review once: related tickets under one epic, requirements and plan drafted
before the hand-over (approved together), blocking questions asked during refinement, `move` read from orch."""
from pathlib import Path

from orch.core.rules import render_rules

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _skill(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_rules_tell_agents_to_batch_the_humans_review(ws):
    text = render_rules(ws.config)
    assert "draft the Plan during refinement" in text and "approves requirements and plan together" in text
    assert "group them under an epic" in text
    assert "ask blocking questions during refinement" in text


def test_refine_skill_drafts_the_plan_and_asks_early():
    text = _skill("orch-refine-ticket")
    assert "**Plan**" in text and "Approve requirements and plan" in text
    assert "during refinement, not mid-work" in text
    assert "group them under an epic" in text


def test_work_skill_knows_a_plan_approved_with_the_requirements():
    text = _skill("orch-work-on-ticket")
    assert "approved together with the requirements" in text
    assert "`move`" in text


def test_tickets_skill_points_to_the_one_review():
    text = _skill("orch-tickets")
    assert "group them under an epic" in text and "`move`" in text
