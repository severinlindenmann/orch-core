"""The CLI's only signing gesture, exercised through a real terminal."""

import os
import select
import termios
import threading
import time

import pytest

from orch.ops import human
from orch.ops.errors import OrchError


def prompt(monkeypatch, keys, *, fail=None):
    master, slave = os.openpty()
    saved = termios.tcgetattr(slave)
    monkeypatch.setattr(human, "open_tty", lambda flags: os.dup(slave))
    real_read = os.read
    if fail:

        def read(fd, n):
            if fd not in (master, slave):
                if fail == "eof":
                    return b""
                raise OSError("disconnected")
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", read)

    def type_keys():
        shown = b""
        while b"any other key cancels." not in shown:
            if not select.select([master], [], [], 5)[0]:
                return
            shown += real_read(master, 4096)
        start = time.monotonic()
        for at, key in keys:
            time.sleep(max(0, start + at - time.monotonic()))
            os.write(master, key)

    person = threading.Thread(target=type_keys)
    person.start()
    try:
        result = human.review_prompt("Review this action\n")
        restored = termios.tcgetattr(slave)
        # Darwin sets PENDIN when switching back to canonical processing.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        saved[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == saved
        return result
    finally:
        person.join(timeout=3)
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    "key, expected", [(b"\r", True), (b"\n", True), (b"x", False), (b"\x03", False), (b"\x04", False)]
)
def test_review_one_key(monkeypatch, key, expected):
    assert prompt(monkeypatch, [(1.2, key)]) is expected


def test_review_early_enter_is_discarded(monkeypatch):
    assert prompt(monkeypatch, [(0.5, b"\r"), (1.2, b"x")]) is False


def test_review_fresh_enter_after_early_enter(monkeypatch):
    assert prompt(monkeypatch, [(0.5, b"\r"), (1.2, b"\r")]) is True


def test_review_held_enter_cancels(monkeypatch):
    assert prompt(monkeypatch, [(i / 20, b"\r") for i in range(2, 26)]) is False


@pytest.mark.parametrize("failure", ["eof", "error"])
def test_review_read_failure_cancels_and_restores_tty(monkeypatch, failure):
    assert prompt(monkeypatch, [(0.2, b"x")], fail=failure) is False


def test_review_no_tty_refuses(monkeypatch):
    def missing(flags):
        raise OSError("no controlling terminal")

    monkeypatch.setattr(human, "open_tty", missing)
    with pytest.raises(OrchError) as error:
        human.review_prompt("review")
    assert error.value.code == "tty.required"


def test_removed_confirmation_interfaces_are_absent():
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[4]
    banned = {"confirm" + "_word", "_confirm" + "_word", "TTY" + "_WORD", "sign" + "_methods"}
    for root in (repo / "packages/core/src", repo / "packages/hub/src", repo / "contracts", repo / "docs"):
        for path in root.rglob("*"):
            if path.suffix not in (".py", ".json", ".md"):
                continue
            text = path.read_text()
            assert not any(name in text for name in banned), path
            if path.suffix == ".py":
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        assert node.name != "_" + "confirm", path
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "review_prompt"
                    ):
                        assert len(node.args) == 1 and not node.keywords, path


def test_pipe_and_yes_cannot_sign_without_tty(tmp_path):
    import subprocess
    import sys

    log = tmp_path / "events.jsonl"
    log.write_text("")
    code = """
import sys
from pathlib import Path
from orch.ops.human import review_prompt
from orch.ops.errors import OrchError
try:
    if review_prompt("Sign an event"):
        Path(sys.argv[1]).write_text("signed\\n")
except OrchError as e:
    print(e.code)
    sys.exit(3)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(log), "--yes"],
        input="\n",
        text=True,
        capture_output=True,
        start_new_session=True,
        timeout=30,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "tty.required"
    assert log.read_text() == ""
