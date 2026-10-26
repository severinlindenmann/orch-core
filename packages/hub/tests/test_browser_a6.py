"""A6 from a real browser (orch-core#858): person home and admin console. Skipped without node Playwright.

Run under the playwright lease on the dev laptop: ``~/orch-dev/scripts/lease.sh run playwright a6 -- uv run pytest tests/test_browser_a6.py -q``
(set ``SHOT_DIR`` to keep desktop + 390 px screenshots).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from channels_helpers import PW
from custodial_helpers import LOG, WK
from test_browser_s5 import NODE, playwright_path
from test_serve_wired import WS, host_link, hub  # noqa: F401  (the wired hub fixture)
from view_helpers import OWNER, Host

from orch_hub.access import ADMIN_GROUP, Access
from orch_hub.channels.service import totp_code
from orch_hub.contract import vectorgen as G

pytestmark = pytest.mark.slow


@pytest.mark.skipif(playwright_path() is None, reason="playwright for node is not installed")
def test_a6_home_and_admin_console_in_a_browser(hub):  # noqa: F811
    c = host_link(hub)
    h = Host(ws=WS, wk=WK, key=G.K.priv["wsk_a"])
    h.genesis()
    h.append("workspace", "member.added", roster_v=1)
    h.ticket()
    h.ask(to=OWNER, text="CSV or API?", options=[{"key": "csv", "label": "CSV"}, {"key": "api", "label": "API"}])
    for log in ("workspace", LOG):
        assert "seq" in c.request("log_sync", h.page(log))
    acc = Access(str(hub["state"] / "access.db"))
    acc.add_member(ADMIN_GROUP, hub["user"])  # the signed-in person is a hub-admin, so the Admin tab shows
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
    script = Path(__file__).parent / "js" / "browser_a6.mjs"
    r = subprocess.run([NODE, str(script)], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
    assert "A6 browser: ok" in r.stdout
    c.close()
