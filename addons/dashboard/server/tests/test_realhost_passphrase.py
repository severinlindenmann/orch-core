"""realhost_kit has no default passphrase: it reads the one `orch setup` generated from <base>/passphrase-<dir>."""

import re

import pytest
from realhost_kit import Workspace, confirm_rules


def test_missing_file_fails_loudly(tmp_path):
    ws = Workspace(tmp_path / "ws", dirname="billing")
    with pytest.raises(RuntimeError, match="passphrase-billing"):
        _ = ws.passphrase
    with pytest.raises(RuntimeError, match="no generated passphrase"):
        ws.rules()[0][1](re.match(".*", ""), "")


def test_reads_the_generated_file_lazily_and_privately(tmp_path):
    ws = Workspace(tmp_path / "ws", dirname="billing")
    rule = ws.rules()[0]  # built before setup has made the passphrase
    (ws.base).mkdir()
    ws.passphrase_file.write_text("generated-words-1234\n")
    assert ws.passphrase == "generated-words-1234"
    assert rule[1](None, "Passphrase: ") == "generated-words-1234"
    ws.passphrase = "other-9"  # what setup_driver does
    assert ws.passphrase_file.read_text() == "other-9\n" and ws.passphrase_file.stat().st_mode & 0o777 == 0o600


def test_empty_file_and_no_constant(tmp_path):
    import realhost_kit

    assert not hasattr(realhost_kit, "PASSPHRASE")
    ws = Workspace(tmp_path / "ws")
    ws.base.mkdir()
    ws.passphrase_file.write_text("\n")
    with pytest.raises(RuntimeError, match="empty"):
        _ = ws.passphrase
    assert confirm_rules("fixed")[0][1](None, "") == "fixed"
