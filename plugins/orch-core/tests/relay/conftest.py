"""Close every relay connection a test opened through ``helpers.session`` (no unclosed loopback sockets)."""

from __future__ import annotations

import pytest

from .helpers import OPEN_CONNS


@pytest.fixture(autouse=True)
def _close_relay_conns():
    yield
    while OPEN_CONNS:
        OPEN_CONNS.pop().close()
