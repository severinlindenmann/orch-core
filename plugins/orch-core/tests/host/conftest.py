"""Host test fixtures shared by every test module under tests/host."""

from __future__ import annotations

import pytest

from tests.host.tier_b_helpers import made, pins_dir, tree  # noqa: F401 - fixtures of the tier B tests


@pytest.fixture(autouse=True)
def _fresh_snapshot_store(monkeypatch):
    """Each test gets its own page snapshot store: ``orch.host.pages.STORE`` is process-wide (H6b), and an xdist worker
    runs many tests, whose workspaces may share an id."""
    from orch.host import pages

    monkeypatch.setattr(pages, "STORE", pages.SnapshotStore())
