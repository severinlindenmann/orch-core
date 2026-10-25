"""H1 L9: the terminal card line in tier B (spec 13.5): "This terminal runs as orch-agent-<person>. It cannot read
your keys." Only a well-formed agent user name from the host is shown; anything else keeps tier A's line."""

import pytest

from orch_dashboard.terminals import execution_line


def test_tier_b_line_names_the_agent_user():
    assert (
        execution_line("orch-agent-sev")
        == "This terminal runs as orch-agent-sev. It cannot read your keys. Never type secrets into it."
    )


@pytest.mark.parametrize("bad", [None, "", "sev", "root", "orch-agent-", "orch-agent-Sev", "orch-agent-a b",
                                 "orch-agent-a\n", "orch-agent-<b>x", 5, ["orch-agent-sev"]])  # fmt: skip
def test_anything_else_shows_no_tier_b_line(bad):
    assert execution_line(bad) is None


def test_preview_view_merges_the_tier_b_line_and_clears_as_person():
    from orch_dashboard.terminals import preview_view

    base = {"spec": "a" * 64, "as_person": True, "notice": "n", "env": []}
    b = preview_view({**base, "as_person": False, "runs_as": "orch-agent-sev"})
    assert b["as_person"] is False and b["execution_line"].startswith("This terminal runs as orch-agent-sev.")
    a = preview_view(base)  # tier A: the host's answer is passed through, no tier B line
    assert a["as_person"] is True and a["execution_line"] is None
    forged = preview_view({**base, "runs_as": "root"})  # a name of the wrong shape never reaches the page
    assert forged["as_person"] is True and forged["execution_line"] is None
