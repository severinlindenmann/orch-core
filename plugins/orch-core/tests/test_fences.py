"""The one fence rule (orch.core.fences) and that the section splitter agrees with markdown-it about it."""
import pytest

from orch.core import evidence, fences
from orch.core.body import split_body
from orch.core.model import Ticket, _split_body, neutral_text, section_text_problem

FOUR = "````"
THREE = "```"


def test_rule():
    assert fences.opening("```") == "```" and fences.opening("   ~~~~ py") == "~~~~"
    assert fences.opening("    ```") is None  # 4 spaces: indented code, not a fence
    assert fences.opening("```a`b") is None  # a backtick fence's info may not hold a backtick
    assert fences.opening("~~~a`b") == "~~~"
    assert fences.closes("````", "```") and not fences.closes("```", "````")
    assert not fences.closes("~~~", "```") and not fences.closes("``` x", "```") and fences.closes("```  ", "```")
    assert fences.info("```orch  ") == "orch"


def test_longer_fence_keeps_a_shorter_one_as_content():
    body = f"## Context\n\n{FOUR}\n{THREE}\n## Log\n{THREE}\n{FOUR}\n\n## Findings\n\nx"
    sections, _ = _split_body(body)
    assert set(sections) == {"Context", "Findings"} and "## Log" in sections["Context"]
    assert section_text_problem(f"{FOUR}\n{THREE}\n## Log\n{FOUR}") is None
    assert "not closed" in section_text_problem(f"{FOUR}\n{THREE}")


def test_body_and_evidence_use_the_same_rule():
    ask, parts = split_body(f"do it\n{FOUR}\n{THREE}\n## Summary\n{THREE}\n{FOUR}\n## Summary\n- one")
    assert "## Summary" in ask and parts == {"Summary": "- one"}
    t = Ticket(meta={}, sections={"Acceptance criteria": "- [ ] a", "Verification":
                                  f"- AC1: ran the suite twice\n{FOUR}\n{THREE}\n- AC9 not a line\n{FOUR}"})
    [c] = evidence.criteria(t)
    assert "AC9 not a line" in c.evidence[0]


def test_neutral_text_escapes_any_fence_like_line():
    assert neutral_text("````x\n ~~~") == "\\````x\n \\~~~"


@pytest.mark.parametrize("text", [
    f"{FOUR}\n{THREE}\n## Inside\n{THREE}\n{FOUR}\n## After",
    f"~~~\n{THREE}\n## Inside\n~~~\n## After",
    f"{THREE}a`b\n## After",
    f"{THREE}\n## Inside\n{THREE} not a close\n{THREE}\n## After",
    f"   {THREE}\n## Inside\n{THREE}\n## After",
    f"    {THREE}\n## After",
])
def test_splitter_agrees_with_markdown_it(text):
    md = pytest.importorskip("markdown_it").MarkdownIt("commonmark")
    headings = [t.content for i, t in enumerate(md.parse(text)) if t.type == "inline"
                and md.parse(text)[i - 1].type == "heading_open"]
    sections, _ = _split_body(text)
    assert list(sections) == headings
