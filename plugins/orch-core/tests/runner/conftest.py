"""Runner fixtures. Every process a test here signals is one the test started (kill only own PIDs)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orch.runner import procs
from orch.runner.supervise import Supervisor
from tests.host.tier_b_helpers import pins_dir, tree  # noqa: F401 - fixtures of the tier B tests
from tests.runner.helpers import wait_for


@pytest.fixture
def state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def sup(state: Path):
    s = Supervisor(state, cgroups=None, scan_interval=0.1, exit_grace=1.0)
    s.open()
    try:
        yield s
    finally:
        s.close(stop_runs=True, grace=0.5)


@pytest.fixture
def sleeper():
    """A process of the test's own, in its own session (never part of a run)."""
    started: list[subprocess.Popen] = []

    def make() -> subprocess.Popen:
        p = subprocess.Popen(["sleep", "60"], start_new_session=True)
        started.append(p)
        wait_for(lambda: procs.info(p.pid))
        return p

    yield make
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait()
