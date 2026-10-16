"""T2 skeleton for B4-H against the REAL orch-relay (loopback, a fake APNs inside the relay process): a linked workspace
with a paired reference phone that registered a push token; a question asked on the host becomes a sealed push, the
host POSTs it with ``Orch-Holder``, the relay's fake APNs receives the body and the outer opens with the workspace's
``WK`` (``orch.crypto.push.open_push``); a 409 ``stale_epoch`` after a rotation reseals; a relay without push answers
404 and the host reports ``push.state = off_at_relay``.

This file is a skeleton: the lead completes it once the relay side (B4-R, ``POST /v2/workspaces/{ws}/push`` and the
fake APNs transport) is in the pinned checkout. Until the pinned relay serves the route it skips; the host-side unit
tests (tests/relay/test_push.py) already cover the send classes against a fake relay."""

from __future__ import annotations

from pathlib import Path

import pytest

from .harness import RelayProcess

pytestmark = pytest.mark.relay_t2


def _relay_serves_push(relay_dir: Path) -> bool:
    for path in sorted((relay_dir / "src" / "orch_relay").rglob("*.py")):
        if "/workspaces/{ws}/push" in path.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def test_a_question_on_the_host_reaches_the_fake_apns(relay: RelayProcess):
    if not _relay_serves_push(relay.dir):
        pytest.skip("the pinned relay has no POST /v2/workspaces/{ws}/push route yet (B4-R)")
    pytest.skip(
        "skeleton: link, pair, register a push token, ask a question, read the fake APNs (the lead completes it)"
    )
