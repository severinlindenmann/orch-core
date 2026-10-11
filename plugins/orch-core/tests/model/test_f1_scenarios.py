"""The signed-event, genesis, device, revocation and restore vectors replayed through ``orch.model`` with the real
``CryptoVerifier`` (ticket-format §5.3, §5.10, §5.11). Each scenario's expected refusals come from the oracle."""

import pytest

from .f1_runner import load, run_scenario

FILES = {
    "replay.json": lambda d: [d],
    "devices.json": lambda d: d["scenarios"],
    "revocation.json": lambda d: d["scenarios"],
    "restore.json": lambda d: d["scenarios"],
    "refusal_order.json": lambda d: d["scenarios"],
    "generation.json": lambda d: d["scenarios"],
    "status.json": lambda d: d["scenarios"],
    "effective_policy.json": lambda d: d["scenarios"],
    "questions.json": lambda d: d["scenarios"],
    "approvals.json": lambda d: d["scenarios"],
}
SCENARIOS = [(f, s) for f, pick in FILES.items() for s in pick(load(f))]


def _param(f, s):
    return pytest.param(f, s, id=f"{f}:{s['name']}")


@pytest.mark.parametrize("f,sc", [_param(f, s) for f, s in SCENARIOS])
def test_scenario(f, sc):
    run_scenario(sc)
