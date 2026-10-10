import pytest

from tests.ops.helpers import Cli, Ws


@pytest.fixture
def ws(tmp_path):
    w = Ws(tmp_path)
    w.bootstrap()
    yield w
    if w.store is not None:
        w.store.close()


@pytest.fixture
def cli(ws):
    return Cli(ws)


@pytest.fixture
def anon(ws):
    """The same workspace without a grant: an unattended agent (and reads that see only workspace tickets)."""
    return Cli(ws, grant=False)
