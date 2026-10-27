"""Run-11 #12/#13 from a real browser: Services and Workspaces pages. Skipped without node Playwright.

Run under the playwright lease: ``~/orch-dev/scripts/lease.sh run playwright r11 -- uv run pytest tests/test_browser_r11.py -q``
(set ``SHOT_DIR`` to keep desktop + 390 px screenshots).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from channels_helpers import PW
from test_browser_s5 import NODE, playwright_path
from test_serve_wired import host_link, hub  # noqa: F401  (the wired hub fixture)

from orch_hub.access import ADMIN_GROUP, Access
from orch_hub.channels.service import totp_code

pytestmark = pytest.mark.slow


@pytest.mark.skipif(playwright_path() is None, reason="playwright for node is not installed")
def test_services_and_workspaces_pages_in_a_browser(hub):  # noqa: F811
    c = host_link(hub)
    acc = Access(str(hub["state"] / "access.db"))
    acc.add_member(ADMIN_GROUP, hub["user"])
    acc.close()
    while (time.time() % 30) > 20:
        time.sleep(0.5)
    env = {
        **os.environ,
        "NODE_PATH": playwright_path(),
        "HUB_URL": f"http://127.0.0.1:{hub['web'].port}",
        "HUB_USER": hub["user"],
        "HUB_PW": PW,
        "CODE_SIGNIN": totp_code(hub["secret"], int(time.time()) // 30 - 1),
    }
    script = Path(__file__).parent / "js" / "browser_r11.mjs"
    r = subprocess.run([NODE, str(script)], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
    assert "R11 browser: ok" in r.stdout
    c.close()
