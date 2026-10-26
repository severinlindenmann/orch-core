"""The rules file of a Claude Code agent in a ticket work folder: where it lives, what is in it, what is refused."""

from __future__ import annotations

import os

import pytest

from orch.runner import rules
from orch.runner.supervise import SpawnError

RUN = "r_01J0000000000000000000000E"
ARGV = ["claude", "--setting-sources", "user", "--strict-mcp-config", rules.FLAG, rules.PLACEHOLDER, "go"]


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "ws"
    (r / ".orch").mkdir(parents=True)
    (r / ".orch" / "AGENTS.orch.md").write_text("ORCH RULES CODEWORD-7\n")
    (r / "AGENTS.md").write_text("PROJECT RULES\n@/etc/passwd\n")
    return r


def test_the_placeholder_is_replaced_by_a_private_file_with_both_rule_files_as_plain_text(root, tmp_path):
    base = tmp_path / "state"
    argv = rules.prepare(base, RUN, ARGV, {"ORCH_WORKSPACE": str(root)})
    path = argv[ARGV.index(rules.PLACEHOLDER)]
    assert argv[:5] == ARGV[:5] and argv[-1] == "go" and path.startswith(str(base / "agent-rules"))
    assert (
        oct(os.stat(path).st_mode & 0o777) == "0o600" and oct(os.stat(base / "agent-rules").st_mode & 0o777) == "0o700"
    )
    text = open(path).read()
    assert "CODEWORD-7" in text and "PROJECT RULES" in text and "@/etc/passwd" in text  # an import stays text
    assert text.index("CODEWORD-7") < text.index("PROJECT RULES")
    rules.remove(base, RUN)
    assert not os.path.exists(path)


def test_an_argv_without_the_placeholder_is_untouched(root, tmp_path):
    assert rules.prepare(tmp_path, RUN, ["claude", "go"], {}) == ["claude", "go"]


@pytest.mark.parametrize(
    "argv",
    [
        ["claude", rules.PLACEHOLDER, "go"],  # not after the flag
        ["claude", rules.FLAG, rules.PLACEHOLDER, rules.PLACEHOLDER],  # twice
        [rules.PLACEHOLDER],
    ],
)
def test_a_misplaced_placeholder_is_refused(root, tmp_path, argv):
    with pytest.raises(SpawnError) as e:
        rules.prepare(tmp_path, RUN, argv, {"ORCH_WORKSPACE": str(root)})
    assert e.value.code == "invalid.input"


def test_no_workspace_is_refused(tmp_path):
    with pytest.raises(SpawnError):
        rules.prepare(tmp_path, RUN, ARGV, {})
    with pytest.raises(SpawnError):
        rules.prepare(tmp_path, RUN, ARGV, {"ORCH_WORKSPACE": "relative/ws"})


def test_the_file_is_bounded_and_links_are_not_followed(root, tmp_path):
    (root / "AGENTS.md").write_text("x" * (200 << 10))
    text = rules.build(str(root))
    assert len(text.encode()) <= rules.LIMIT + 100 and "CODEWORD-7" in text
    os.unlink(root / ".orch" / "AGENTS.orch.md")
    (tmp_path / "secret.md").write_text("SECRET")
    os.symlink(tmp_path / "secret.md", root / ".orch" / "AGENTS.orch.md")
    assert "SECRET" not in rules.build(str(root))
