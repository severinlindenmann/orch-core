"""Run 12, "switch is navigation; authority stays with the workspace", in a real browser (Chromium via node Playwright)
with the REAL hub web API and the REAL dashboard server + built single-page app on two ports (two origins).

* A browser that signed in on this computer enrols a WebCrypto key (IndexedDB); a hub switch in that same browser lands
  FULL with no prompt.
* A fresh profile lands read-mostly: reads work, every change gets the structured `step_up_required`.
* That hub-made session, replayed from the trusted browser (same machine, loopback), still cannot change anything.
* Revoking the browser ends the session it upgraded.

Needs the built app (`npm run build` in packages/addons/dashboard) and Playwright for node; skipped without them.
Run under the playwright lease: ``~/orch-dev/scripts/lease.sh run playwright switchauth -- uv run pytest tests/test_switch_authority_browser.py -q``
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from test_browser_s5 import NODE, playwright_path
from test_switch_two_origins import DASH_PY, WS, build_stack, login_hub, ticket_of

STATIC = Path(__file__).resolve().parents[2] / "addons/dashboard/server/src/orch_dashboard/static"
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(playwright_path() is None, reason="playwright for node is not installed"),
    pytest.mark.skipif(
        not (STATIC / "index.html").exists() or not DASH_PY.exists(), reason="the dashboard is not built"
    ),
]


def test_switch_authority_in_a_real_browser(tmp_path):
    gen = build_stack(tmp_path, static_dir=STATIC)
    st = next(gen)
    try:
        login_hub(st, st.w.ann)
        st.dash.call("GET", f"/api/w/{WS}/hub/rail")  # the dashboard tells the hub where it lives
        tickets = []
        for _ in range(2):  # one for the trusted browser, one for a fresh profile
            s, o = st.hub.call("POST", "/api/v1/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "open"})
            assert s == 200, o
            tickets.append(ticket_of(o["url"]))
        st.proc.stdin.write("launch\n")
        st.proc.stdin.flush()
        line = st.proc.stdout.readline()
        assert line.startswith("CODE "), line
        env = {
            **os.environ,
            "NODE_PATH": playwright_path(),
            "DASH_ORIGIN": st.dash_origin,
            "LAUNCH_CODE": line.split()[1],
            "TICKET_TRUSTED": tickets[0],
            "TICKET_FRESH": tickets[1],
            "WS": WS,
        }
        script = Path(__file__).parent / "js" / "browser_switch_authority.mjs"
        r = subprocess.run([NODE, str(script)], env=env, capture_output=True, text=True, timeout=120, check=False)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
        assert "switch authority browser: ok" in r.stdout
    finally:
        for _ in gen:  # tear the stack down
            pass
