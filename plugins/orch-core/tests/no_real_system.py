"""No test outside the Linux-root ones may depend on the machine's real users and pins (#1258).

``service.agent_tier`` read ``RealOsFacts()``, so a root test on another xdist worker that had just made
``orch-agent-<user>`` turned every click-runner test into "agents run as their own user ... not safe". The default is
now an empty machine (no such user: tier A). A test that wants a tier passes ``facts=`` or replaces ``agent_tier``.
Imported as an autouse fixture by the conftest of each directory whose tests reach the runner CLI."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_system_facts():
    from orch.runner import service
    from tests.osfacts_fake import FakeOsFacts

    real = service.agent_tier

    def isolated(state, uid=None, *, facts=None):
        return real(state, uid, facts=facts if facts is not None else FakeOsFacts("linux"))

    # Not through ``monkeypatch``: that fixture would then be set up before every other and undone after them, which
    # changes when a test's own patches are undone relative to its fixtures' teardown.
    service.agent_tier = isolated
    # The same for the L13 isolation doctor behind ``orch host status``: an empty machine, no real file read.
    from orch.host import doctor_isolation, status

    real_facts, real_read = status._default_facts, doctor_isolation.read_text
    status._default_facts = lambda: FakeOsFacts("linux")
    doctor_isolation.read_text = lambda path: None
    try:
        yield
    finally:
        service.agent_tier = real
        status._default_facts, doctor_isolation.read_text = real_facts, real_read
