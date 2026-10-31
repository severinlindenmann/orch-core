"""#1823: when the signer refuses a request because ``review_digest`` does not match the material, the test helper
prints which part of the material moved (received vs the host's stored request). Test helper only: the product signer
never logs material values."""

from __future__ import annotations

import pytest

from orch.host import signer
from tests.host import human_helpers

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


def test_a_digest_mismatch_prints_both_digests_and_the_keys_that_moved(human, served, capsys):
    _, key = human.ticket()
    prepared = human.prepare("log", {"ref": key, "text": "x"})
    host_digest = prepared["review_digest"]
    prepared["review_digest"] = "0" * len(host_digest)  # what a host that hashed other material would have sent
    with pytest.raises(signer.SignerRefused, match="review_digest does not match"):
        human.check(prepared)
    out = capsys.readouterr().out
    assert "#1823 review_digest trace" in out
    assert "top-level keys that differ (received vs stored): []" in out  # the material itself arrived unchanged
    assert f"digest(received)     = {host_digest}" in out and f"digest(stored record) = {host_digest}" in out


def test_no_trace_when_the_signer_refuses_for_another_reason(human, served, capsys):
    _, key = human.ticket()
    prepared = human.prepare("log", {"ref": key, "text": "x"})
    prepared["workspace_id"] = "0" * 32
    with pytest.raises(signer.SignerRefused):
        human.check(prepared)
    assert "#1823" not in capsys.readouterr().out
