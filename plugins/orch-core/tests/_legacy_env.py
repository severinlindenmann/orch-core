"""Environment entries that make a child process of the suite open legacy root stores (see ``_legacy_shim``)."""

from pathlib import Path

ENV = {"PYTHONPATH": str(Path(__file__).parent / "_legacy_shim"), "ORCH_TEST_LEGACY_LAYOUT": "1"}
