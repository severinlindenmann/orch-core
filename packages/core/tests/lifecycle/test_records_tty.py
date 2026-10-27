"""Records uses the common terminal review."""
import pytest

from orch.records import cli as records_cli


@pytest.mark.parametrize("ok", [True, False])
def test_records_uses_review_prompt(monkeypatch, ok):
    asked = []
    monkeypatch.setattr("orch.ops.human.review_prompt", lambda text: asked.append(text) or ok)
    assert records_cli._tty_confirm("Turn it on?") is ok
    assert asked == ["Turn it on?"]
