"""Close every loopback connection a test opened (no unclosed sockets), as ``tests/relay/conftest.py``."""

from __future__ import annotations

import pytest

from .kit import OPEN


@pytest.fixture(autouse=True)
def _close_conns():
    yield
    while OPEN:
        OPEN.pop().close()
