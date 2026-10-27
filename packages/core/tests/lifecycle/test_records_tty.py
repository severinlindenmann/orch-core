"""run-12 O12: `orch records auto on` asks [y/N] on the real terminal (no typed word), same reader as prune."""

from __future__ import annotations

import pytest

from orch.records import cli as records_cli


@pytest.mark.parametrize(
    ("answer", "ok"), [("y", True), ("Y", True), ("yes", True), ("", False), ("n", False), ("no", False)]
)
def test_tty_confirm_is_a_y_n_prompt_on_dev_tty(monkeypatch, answer, ok):
    asked = []
    monkeypatch.setattr("orch.prune.safe.tty_ask", lambda prompt: asked.append(prompt) or answer)
    assert records_cli._tty_confirm("Turn it on?") is ok
    assert asked and "[y/N]" in asked[0] and "Type yes" not in asked[0]
