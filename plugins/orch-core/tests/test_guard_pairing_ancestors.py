from pathlib import Path

import pytest

from orch.hooks.guard import _reaches_pairing_keys


@pytest.mark.parametrize("cmd", [
    "find / -name 'remote*'",
    f"tar czf /tmp/a.tgz {Path.home().parent}",
    "grep -r key ~/..",
])
def test_recursive_reads_from_higher_ancestors_are_denied(cmd):
    assert _reaches_pairing_keys(cmd)


@pytest.mark.parametrize("cmd", ["ls src/*.py", "cat /etc/hosts", "uv run pytest -q tests/", "rg foo src/", "ls /"])
def test_ordinary_commands_stay_allowed(cmd):
    assert not _reaches_pairing_keys(cmd)
