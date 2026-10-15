"""Every code the store can raise maps to a declared CLI error (no StoreError escapes as an undeclared code)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from orch.cli.store_errors import TABLE, code_for, to_orch_error
from orch.model import Code
from orch.ops.errors import ERRORS, GLOBAL_ERRORS
from orch.store import StoreError

STORE_SRC = Path(__file__).resolve().parents[2] / "src" / "orch" / "store"


def literal_codes() -> set[str]:
    out: set[str] = set()
    for p in STORE_SRC.glob("*.py"):
        out |= set(re.findall(r'StoreError\(\s*"([a-z_]+\.[a-z_]+)"', p.read_text()))
    return out | {"chain.diverged", "store.busy", "store.torn_write"}


def test_every_code_the_store_raises_is_in_the_table():
    codes = literal_codes()
    assert {"validation.host_state", "validation.path", "store.busy", "store.read_only", "chain.broken"} <= codes
    for code in sorted(codes):
        assert code in TABLE or code.startswith(("validation.", "body.")), f"{code} is not mapped"


@pytest.mark.parametrize("code", sorted({c.value for c in Code} | set(TABLE) | literal_codes()))
def test_each_code_maps_to_a_catalog_entry_and_falls_back_to_a_global_one(code):
    assert code_for(code) in ERRORS
    err = to_orch_error(StoreError(code, "detail"))
    assert err.code in GLOBAL_ERRORS  # an operation that declares nothing still gets a code it may raise
    declared = to_orch_error(StoreError(code, "detail"), declared=[code_for(code)])
    assert declared.code == code_for(code)


def test_model_refusals_keep_their_meaning():
    assert to_orch_error(StoreError("store.busy")).code == "retry.later"
    assert to_orch_error(StoreError("gate.stale"), declared=["gate.stale"]).code == "gate.stale"
    assert to_orch_error(StoreError("status.transition"), declared=["transition.refused"]).code == "transition.refused"
    assert to_orch_error(StoreError("ticket.unknown"), declared=["not_found"]).code == "not_found"
    assert to_orch_error(StoreError("chain.broken", "x")).code == "internal"
    assert to_orch_error(StoreError("validation.body", "x")).code == "invalid.input"
    assert to_orch_error(StoreError("something.new")).code == "internal"  # unknown: a fault, never a pass
