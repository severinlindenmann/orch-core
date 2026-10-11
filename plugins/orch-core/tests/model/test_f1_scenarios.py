"""The signed-event, genesis, device, revocation and restore vectors replayed through ``orch.model`` with the real
``CryptoVerifier`` (ticket-format §5.3, §5.10, §5.11). Each scenario's expected refusals come from the oracle."""

import pytest

from .f1_runner import load, run_scenario

FILES = {
    "replay.json": lambda d: [d],
    "devices.json": lambda d: d["scenarios"],
    "revocation.json": lambda d: d["scenarios"],
    "restore.json": lambda d: d["scenarios"],
    "generation.json": lambda d: d["scenarios"],
    "status.json": lambda d: d["scenarios"],
    "effective_policy.json": lambda d: d["scenarios"],
    "questions.json": lambda d: d["scenarios"],
}
SCENARIOS = [(f, s) for f, pick in FILES.items() for s in pick(load(f))]


# Gaps in orch.model that the vectors expose (ticket-format 5.10 is clear, the model does not enforce it yet):
# ``restore`` lists ``abandoned_decisions`` but nothing refuses the same signed event on the new chain.
KNOWN_GAPS = {"questions.json:question_replayed_decision_after_restore": "abandoned_decisions is not enforced"}


def _param(f, s):
    key = f"{f}:{s['name']}"
    marks = [pytest.mark.xfail(strict=True, reason=KNOWN_GAPS[key])] if key in KNOWN_GAPS else []
    return pytest.param(f, s, id=key, marks=marks)


@pytest.mark.parametrize("f,sc", [_param(f, s) for f, s in SCENARIOS])
def test_scenario(f, sc):
    run_scenario(sc)
