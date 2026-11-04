import pytest

from orch.migrate import backup as bk


@pytest.fixture(autouse=True)
def _no_time_machine(monkeypatch):
    monkeypatch.setattr(bk, "_exclude_from_time_machine", lambda p: None)  # tmutil can hang on a test machine
