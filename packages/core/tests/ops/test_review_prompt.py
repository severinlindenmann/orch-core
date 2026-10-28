"""The CLI's only signing gesture, exercised through a real terminal."""

import os
import select
import termios
import threading
import time

import pytest

from orch.ops import human
from orch.ops.errors import OrchError


def prompt(monkeypatch, keys, *, fail=None, text="Review this action\n", seen=None, **kw):
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
            while (left := start + at - time.monotonic()) > 0:  # keep reading what the prompt writes meanwhile
                if select.select([master], [], [], left)[0]:
                    shown += real_read(master, 4096)
            os.write(master, key)
        while select.select([master], [], [], 0.3)[0]:
            shown += real_read(master, 4096)
        if seen is not None:
            seen.append(shown.decode())

    person = threading.Thread(target=type_keys)
    person.start()
    try:
        result = human.review_prompt(text, **kw)
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


def _review():
    from orch.host.signer import Review

    out = Review("Approve the plan\n")
    out.details = "Approve the plan\nevent id: 01J9ZK00000000000000000001\nhash: sha256:abc\n"
    return out


def test_review_d_shows_the_details_then_enter_signs(monkeypatch):
    seen = []
    assert prompt(monkeypatch, [(1.2, b"d"), (2.6, b"\r")], text=_review(), seen=seen) is True
    (shown,) = seen
    assert "Press Enter to sign, d for details, any other key cancels." in shown
    after = shown.split("d for details", 1)[1]
    assert "hash: sha256:abc" in after and "event id: 01J9ZK00000000000000000001" in after
    assert after.rstrip().endswith("Press Enter to sign, any other key cancels.")


def test_review_d_then_an_early_enter_is_discarded_and_another_key_cancels(monkeypatch):
    # a new dead time after the details: an Enter right after them is dropped, the next key decides
    assert prompt(monkeypatch, [(1.2, b"d"), (1.5, b"\r"), (2.6, b"x")], text=_review()) is False


def test_review_d_twice_cancels(monkeypatch):
    assert prompt(monkeypatch, [(1.2, b"d"), (2.6, b"d")], text=_review()) is False


def test_a_confirmation_that_signs_nothing_says_continue_and_has_no_details(monkeypatch):
    seen = []
    assert prompt(monkeypatch, [(1.2, b"\r")], text=_review(), seen=seen, verb="continue") is True
    assert "Press Enter to continue, any other key cancels." in seen[0] and "sign" not in seen[0]
    assert prompt(monkeypatch, [(1.2, b"d")], text=_review(), verb="continue") is False


def test_a_signing_text_without_details_offers_no_d(monkeypatch):
    seen = []
    assert prompt(monkeypatch, [(1.2, b"d")], seen=seen) is False  # `d` is just another key: it cancels
    assert "Press Enter to sign, any other key cancels." in seen[0] and "d for details" not in seen[0]


def test_review_verb_is_sign_or_continue():
    with pytest.raises(ValueError):
        human.review_prompt("x", verb="approve")


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
    banned = {"confirm" + "_word", "_confirm" + "_word", "TTY" + "_WORD"}
    # B6 retired: `sign_methods` is accepted and ignored so old One click logs replay. It may be NAMED only where it is
    # accepted (the two schemas), explained (comments, the architecture doc) or pinned (the F1 vector); no source file
    # holds it as a value, so nothing can write or read it.
    old_key = "sign" + "_methods"
    may_name = {
        "packages/core/src/orch/schema/schemas/event.settings.changed.json",
        "packages/core/src/orch/schema/schemas/workspace.json",
        "packages/core/src/orch/model/workspace.py",
        "packages/core/src/orch/hubclient/custodial_shape.py",
        "contracts/core/f1/refusal_order.json",
        "contracts/core/f1/index.json",
        "docs/architecture/orch-v2-host.md",
        "docs/architecture/orch-v2-ticket-format.md",
    }
    for root in (repo / "packages/core/src", repo / "packages/hub/src", repo / "contracts", repo / "docs"):
        for path in root.rglob("*"):
            if path.suffix not in (".py", ".json", ".md"):
                continue
            text = path.read_text()
            assert not any(name in text for name in banned), path
            assert old_key not in text or str(path.relative_to(repo)) in may_name, path
            if path.suffix == ".py":
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Constant) and isinstance(node.value, str):
                        assert old_key not in node.value, path  # comments only: never a key in code
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        assert node.name != "_" + "confirm", path
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "review_prompt"
                    ):
                        assert len(node.args) == 1, path
                        assert all(k.arg == "verb" for k in node.keywords), path


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
