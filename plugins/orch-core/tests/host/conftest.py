"""Host test fixtures shared by every test module under tests/host."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_snapshot_store(monkeypatch):
    """Each test gets its own page snapshot store: ``orch.host.pages.STORE`` is process-wide (H6b), and an xdist worker
    runs many tests, whose workspaces may share an id."""
    from orch.host import pages

    monkeypatch.setattr(pages, "STORE", pages.SnapshotStore())


@pytest.fixture(autouse=True)
def _no_real_service_manager(monkeypatch):
    """No host test ever reaches launchctl or systemctl (H7): ``orch host enable`` and a ``register`` that finds no host
    get a service manager whose every command fails. A test that wants a service installs its own fake
    (``tests/host/test_host_mode_on.py``)."""
    import subprocess

    from orch.host import cli
    from orch.host.enable import Manager

    def refuse(argv):
        return subprocess.CompletedProcess(list(argv), 1, "", "service managers are off in tests")

    monkeypatch.setattr(cli, "SERVICE_MANAGER", lambda unit: Manager(unit, run=refuse))
