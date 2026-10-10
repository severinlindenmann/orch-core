import pytest

from tests.store.helpers import Env


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    if e.store is not None:
        e.store.close()
