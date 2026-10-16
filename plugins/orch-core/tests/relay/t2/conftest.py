"""T2 fixtures: one real relay per test, started twice over (the internal 4 MiB cap and the 16 MiB default)."""

from __future__ import annotations

import signal
import time
from types import ModuleType

import pytest

from ..helpers import OPEN_CONNS
from . import harness
from .harness import LIMITS, TEST_BUDGET_S, RelayProcess, ref_module, relay_dir, start_relay


class T2Timeout(BaseException):  # noqa: N818 - a BaseException: a broad `except Exception` cannot swallow it
    """A T2 test ran over :data:`TEST_BUDGET_S` (raised in the main thread by SIGALRM)."""


def _over_budget(signum: int, frame: object) -> None:
    raise T2Timeout(f"T2 test over its {TEST_BUDGET_S:.0f} s budget")


class _Budget:
    """A hard per-test limit on ITIMER_REAL; an outer alarm already armed (``perl -e 'alarm N'``) keeps its remaining
    time and handler afterwards."""

    def __init__(self, budget: float | None = None) -> None:
        self.budget = budget  # None: the module's TEST_BUDGET_S, read when the test starts (tests patch it)

    def __enter__(self) -> _Budget:
        self.t0 = time.monotonic()
        self.old_handler = signal.signal(signal.SIGALRM, _over_budget)
        self.old_left, _ = signal.setitimer(signal.ITIMER_REAL, TEST_BUDGET_S if self.budget is None else self.budget)
        return self

    def __exit__(self, *exc: object) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self.old_handler)
        if self.old_left > 0:
            signal.setitimer(signal.ITIMER_REAL, max(0.01, self.old_left - (time.monotonic() - self.t0)))


@pytest.fixture(params=LIMITS, ids=["cap4MiB", "cap16MiB-default"])
def relay(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):
    long = request.node.get_closest_marker("t2_long")  # @pytest.mark.t2_long(seconds): a test that must idle in real time
    if long is not None and request.param is not None:
        pytest.skip("a real-time idle test runs once, against the relay's default cap")
    d = relay_dir()
    data = tmp_path_factory.mktemp("relay-data")
    rp: RelayProcess = start_relay(d, data, request.param)
    try:
        with _Budget(float(long.args[0]) if long is not None else None):
            yield rp
    finally:
        while OPEN_CONNS:  # this test's keep-alive connections first: the relay then has none to wait for
            OPEN_CONNS.pop().close()
        leaked = rp.finish()  # a leak is reaped (our own group only) AND fails the test
    assert not leaked, f"relay teardown leaked (reaped): {leaked}"


@pytest.fixture
def R(relay: RelayProcess) -> ModuleType:
    """The relay's reference client from the pinned checkout (test support only, never vendored)."""
    return ref_module(relay.dir)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    leaks = harness.reap_all()
    if leaks:
        print(f"\nT2: relays still running at session end were reaped: {leaks}")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
