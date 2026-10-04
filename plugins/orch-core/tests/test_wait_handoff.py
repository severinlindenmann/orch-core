"""Issue 4: agents wait with `orch wait` on every hand-off to the human instead of ending on "tell me when"."""
import re
from pathlib import Path

from orch.core.rules import render_rules
from orch.instructions.render import agents_rules

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def _step(skill: str, number: int) -> str:
    text = (SKILLS / skill / "SKILL.md").read_text(encoding="utf-8")
    match = re.search(rf"^{number}\. .*?(?=^\d+\. |\Z)", text, flags=re.M | re.S)
    assert match, f"{skill} has no step {number}"
    return match.group(0)


def test_always_loaded_rules_name_orch_wait(ws):
    rules = render_rules(ws.config)
    assert "waiting on the human:" in rules and "orch wait <id> --json" in rules and "re-run it on timeout" in rules
    agents = agents_rules(ws.config)
    assert "orch wait <id> --json" in agents and "instead of ending on \"tell me when\"" in agents
    assert "on timeout, run it again" in agents


def test_every_hand_off_in_the_skills_waits():
    assert "orch wait <id> --json" in _step("orch-refine-ticket", 7)  # requirements gate
    for number in (2, 4, 8, 9):  # plan gate, questions and every hand-off, testing verdict, changes made on request
        assert "orch wait <id> --json" in _step("orch-work-on-ticket", number), number


def test_a_timeout_re_arms_instead_of_ending_the_work():
    step = _step("orch-work-on-ticket", 4)
    assert "If it times out, run it again" in step
    assert "If it times out, stop" not in step
    assert "run it again" in _step("orch-refine-ticket", 7)
