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
