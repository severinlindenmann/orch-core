import pytest

from tests.store.helpers import Env


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    if e.store is not None:
        e.store.close()


@pytest.fixture
def bound():
    """Bind a handler to an operation for one test and put the original back."""
    import orch.ops as ops

    saved: dict[str, object] = {}

    def bind(name: str, handler) -> None:
        saved.setdefault(name, ops.get(name).handler)
        ops.bind(name, handler)

    yield bind
    for name, handler in saved.items():
        ops.bind(name, handler)
