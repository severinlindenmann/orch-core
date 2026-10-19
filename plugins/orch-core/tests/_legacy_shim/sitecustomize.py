"""Test-only: a child process of the suite (the orch CLI, a worker) opens the legacy root stores the suite's fixtures
build. Production code has no switch for this: it is set here, on the module flag, and only when the suite asks."""

import os

if os.environ.get("ORCH_TEST_LEGACY_LAYOUT") == "1":
    from orch import layout

    layout.LEGACY_ALLOWED = True
