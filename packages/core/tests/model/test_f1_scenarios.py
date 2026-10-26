"""The signed-event, genesis, device, revocation and restore vectors replayed through ``orch.model`` with the real
``CryptoVerifier`` (ticket-format §5.3, §5.10, §5.11). Each scenario's expected refusals come from the oracle."""

import copy

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
    "phone_events.json": lambda d: d["scenarios"],
}
SCENARIOS = [(f, s) for f, pick in FILES.items() for s in pick(load(f))]


def _param(f, s):
    return pytest.param(f, s, id=f"{f}:{s['name']}")


@pytest.mark.parametrize("f,sc", [_param(f, s) for f, s in SCENARIOS])
def test_scenario(f, sc):
    run_scenario(sc)


@pytest.mark.parametrize("field,wrong", [("append_code", "device.scope"), ("expect", "validation.event")])
def test_runner_checks_store_and_model_refusals_separately(field, wrong):
    sc = copy.deepcopy(load("devices.json")["scenarios"][0])
    index = next(i for i, step in enumerate(sc["steps"]) if step.get("schema_refused"))
    sc["steps"] = sc["steps"][: index + 1]
    sc["steps"][-1][field] = wrong
    with pytest.raises(AssertionError, match=f"{sc['name']} step {index}"):
        run_scenario(sc)
