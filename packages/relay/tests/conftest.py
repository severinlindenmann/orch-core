"""Test-wide settings. The Drop routes (P3-D39) are off by default in production (review F1,
ORCH_RELAY_DROP); the suite runs with them on, and tests/test_drop_authz.py checks the off state."""

import os

import pytest

os.environ.setdefault("ORCH_RELAY_DROP", "1")


@pytest.fixture(autouse=True)
def drop_state_dir(tmp_path_factory, monkeypatch):
    """D39a2 part 4: with Drop on, a relay without a valid ORCH_RELAY_STATE serves no Drop route.
    Every test gets its own state directory, outside any data directory it uses."""
    monkeypatch.setenv("ORCH_RELAY_STATE", str(tmp_path_factory.mktemp("state")))


@pytest.fixture(autouse=True)
def _formatted_data(monkeypatch, request):
    """``orch install relay`` leaves ``format.json`` in the data dir and the relay opens it only at
    that format: a data dir that a test names for ``create_app`` gets the same file first. A test
    that wants the real rule on a bare dir says ``raw_data``."""
    if request.node.get_closest_marker("raw_data"):
        return
    from format_helpers import ensure_format

    from orch_relay import app as app_mod

    real = app_mod.open_data

    def open_formatted(path, kind, current, *args, **kw):
        ensure_format(path, kind, current)
        return real(path, kind, current, *args, **kw)

    monkeypatch.setattr(app_mod, "open_data", open_formatted)


@pytest.fixture(autouse=True)
def _no_real_apns(monkeypatch):
    """A test that forgot to inject a transport must never reach Apple: the gateways go nowhere."""
    from orch_relay import apns

    monkeypatch.setattr(
        apns, "GATEWAYS", {"production": "http://127.0.0.1:1", "sandbox": "http://127.0.0.1:1"}
    )
