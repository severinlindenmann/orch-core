"""T2 fixtures: one real relay per test, started twice over (the internal 4 MiB cap and the 16 MiB default)."""

from __future__ import annotations

import time
from types import ModuleType

import pytest

from ..helpers import OPEN_CONNS
from .harness import LIMITS, TEST_BUDGET_S, RelayProcess, ref_module, relay_dir, start_relay


@pytest.fixture(params=LIMITS, ids=["cap4MiB", "cap16MiB-default"])
def relay(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    d = relay_dir()
    data = tmp_path_factory.mktemp("relay-data")
    rp: RelayProcess = start_relay(d, data, request.param)
    t0 = time.monotonic()
    try:
        yield rp
    finally:
        took = time.monotonic() - t0
        while OPEN_CONNS:  # this test's keep-alive connections first: the relay then has none to wait for
            OPEN_CONNS.pop().close()
        rp.stop()
    leaked = rp.leaked()
    assert not leaked, f"relay teardown leaked: {leaked}"
    assert took < TEST_BUDGET_S, f"a T2 test took {took:.1f} s (budget {TEST_BUDGET_S} s)"


@pytest.fixture
def R(relay: RelayProcess) -> ModuleType:
    """The relay's reference client from the pinned checkout (test support only, never vendored)."""
    return ref_module(relay.dir)
