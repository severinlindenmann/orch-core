"""T2 fixtures: one real relay per test, started twice over (the internal 4 MiB cap and the 16 MiB default)."""

from __future__ import annotations

from types import ModuleType

import pytest

from .harness import LIMITS, RelayProcess, ref_module, relay_dir, start_relay


@pytest.fixture(params=LIMITS, ids=["cap4MiB", "cap16MiB-default"])
def relay(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    d = relay_dir()
    data = tmp_path_factory.mktemp("relay-data")
    rp: RelayProcess = start_relay(d, data, request.param)
    try:
        yield rp
    finally:
        rp.stop()
    leaked = rp.leaked()
    assert not leaked, f"relay teardown leaked: {leaked}"


@pytest.fixture
def R(relay: RelayProcess) -> ModuleType:
    """The relay's reference client from the pinned checkout (test support only, never vendored)."""
    return ref_module(relay.dir)
