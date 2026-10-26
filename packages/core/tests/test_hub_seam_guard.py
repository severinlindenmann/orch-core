"""The suite-wide guard of ``tests/conftest.py``: a test that swaps a hub seam by plain assignment and never puts
it back is failed by name and the seam is repaired, so it cannot make later tests in the same xdist worker talk to its
stub.

Why: ``test_publishing`` built a ``World`` (raw swap of ``hubops.TRANSPORT`` / ``CARD_SOURCE`` / ``workspace_dev``) in
a test that never called ``World.close()``. Every hub test that ran after it in that worker failed ``hub.connect`` with
``hub.refused: bad_signature`` (the stale stub hub signs with another key): a flake that
depended on xdist scheduling."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]

INNER = """
from orch.host import breakglass, hubops
from tests.conftest import _hub_seams_never_leak  # noqa: F401  (the guard under test, autouse in this module)


def test_a_leaks_every_seam_by_assignment():
    hubops.TRANSPORT = lambda service, hub_id: "stale hub"
    hubops.CARD_SOURCE = lambda service: {}
    hubops.workspace_dev = lambda service: True
    breakglass.TRANSPORT = object()


def test_b_the_next_test_sees_the_real_seams():
    assert hubops.TRANSPORT is None and hubops.CARD_SOURCE is None and breakglass.TRANSPORT is None
    assert hubops.workspace_dev.__module__ == "orch.host.hubops"


def test_c_monkeypatch_is_fine(monkeypatch):
    monkeypatch.setattr(hubops, "TRANSPORT", lambda service, hub_id: "patched")
"""


def test_a_leaked_hub_seam_fails_that_test_and_is_repaired_for_the_next(tmp_path):
    inner = tmp_path / "test_inner.py"
    inner.write_text(INNER)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly",
         "--rootdir", str(tmp_path), str(inner)],
        cwd=PLUGIN, env={**os.environ, "PYTHONPATH": str(PLUGIN)}, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    out = res.stdout + res.stderr
    assert res.returncode != 0, out
    assert "left a hub seam swapped" in out, out
    for name in ("hubops.TRANSPORT", "hubops.CARD_SOURCE", "hubops.workspace_dev", "breakglass.TRANSPORT"):
        assert name in out, (name, out)
    assert "3 passed, 1 error" in out, out  # a ran and errored in teardown; b and c passed (the leak was repaired)
