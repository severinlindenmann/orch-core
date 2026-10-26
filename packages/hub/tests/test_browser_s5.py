"""S5 from a real browser (design-a8, orch-core#860): sign in, register the browser key, answer a question, signed.

Needs Playwright for node (`NODE_PATH` or a global install) and a Chromium; skipped without them, so GitHub CI (which has
neither) runs the Node/Python unit tests and this one runs under the ``playwright`` lease on the dev laptop:
``~/orch-dev/scripts/lease.sh run playwright a8 -- uv run pytest tests/test_browser_s5.py -q``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from channels_helpers import PW
from custodial_helpers import LOG, WK
from test_serve_wired import WS, host_link, hub  # noqa: F401  (the wired hub fixture)
from view_helpers import OWNER, Host

from orch_hub.channels.service import totp_code
from orch_hub.contract import vectorgen as G

pytestmark = pytest.mark.slow
NODE = shutil.which("node")


def playwright_path() -> str | None:
    if NODE is None or shutil.which("npm") is None:
        return None
    root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True, check=False).stdout.strip()
    env_path = os.environ.get("NODE_PATH", "")
    for base in [p for p in [*env_path.split(os.pathsep), root] if p]:
        if (Path(base) / "playwright").is_dir():
            return base
    return None


@pytest.mark.skipif(playwright_path() is None, reason="playwright for node is not installed")
def test_s5_browser_registers_a_key_and_signs_a_question(hub):  # noqa: F811
    c = host_link(hub)
    h = Host(ws=WS, wk=WK, key=G.K.priv["wsk_a"])
    h.genesis()
    h.append("workspace", "member.added", roster_v=1)
    h.ticket()
    h.ask(to=OWNER, text="CSV or API?", options=[{"key": "csv", "label": "CSV"}, {"key": "api", "label": "API"}])
    for log in ("workspace", LOG):
        assert "seq" in c.request("log_sync", h.page(log))

    # two single-use codes: sign-in takes the previous step, registration the current one (no 30 s wait)
    while (time.time() % 30) > 15:  # keep the whole run inside one step
        time.sleep(0.5)
    step = int(time.time()) // 30
    env = {
        **os.environ,
        "NODE_PATH": playwright_path(),
        "HUB_URL": f"http://127.0.0.1:{hub['web'].port}",
        "HUB_USER": hub["user"],
        "HUB_PW": PW,
        "CODE_SIGNIN": totp_code(hub["secret"], step - 1),
        "CODE_REGISTER": totp_code(hub["secret"], step),
    }
    script = Path(__file__).parent / "js" / "browser_s5.mjs"
    r = subprocess.run([NODE, str(script)], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-3000:]
    assert "S5 browser: ok" in r.stdout
    c.close()
