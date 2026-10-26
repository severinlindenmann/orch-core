"""Bring-up leaves no dashboard server behind: the one `orch setup` starts for its wizard signs with the keychain store,
not the kit's file store (a later caller that used it got KeyNotFound, a 409 "uncertain")."""

import os
import shutil
import subprocess
import uuid

import pytest
from realhost_kit import Workspace, short_run_root

pytestmark = [
    pytest.mark.realhost,
    pytest.mark.skipif(os.name != "posix", reason="a real host needs unix sockets and a pty"),
]


def test_no_setup_dashboard_remains_after_bring_up():
    root = short_run_root() / uuid.uuid4().hex[:6]
    ws = Workspace(root / "w")
    try:
        try:
            ws.bring_up(lambda w: {})
        except (RuntimeError, TimeoutError, OSError) as exc:
            pytest.skip(f"the real host cannot start here: {str(exc)[:300]}")
        assert not ws.dashboard_ctl.exists()
        procs = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True, check=False).stdout
        assert not [ln for ln in procs.splitlines() if "orch_dashboard" in ln and str(ws.state) in ln]
    finally:
        ws.stop_host()
        shutil.rmtree(root, ignore_errors=True)
