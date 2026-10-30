# ruff: noqa: E501
"""R5 T4: the browser run. A real Chromium (own temp profile, mock keychain) drives the REAL built UI (dist) of the home
page and of a workspace session through the in-process client, hub and host of the end-to-end test. Needs the UI built
(``npx vite build`` in packages/addons/dashboard) and Playwright (the kit's e2e/dashboard node_modules); skipped without
them. Screenshots land in ~/orch-dev/state/e2e-shots/r5-e2e-*.png. Run: ORCH_R5_BROWSER=1 under
``lease.sh run heavy-test``."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from orch.canon.jcs import dumps
from orch.crypto.encoding import b64u
from orch.remoteclient import presence as P
from test_remote_client_hub_e2e import OWN_NAME, Origin_, redirect_target, stack, world  # noqa: F401

from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import human, lab, served  # noqa: F401
from tests.host.test_remote_hub_e2e import dev_workspace, hub, hub_kw  # noqa: F401
from tests.remote_client.test_client_e2e import Responder

DASH = Path(__file__).resolve().parents[2]
DIST = DASH / "dist"
KIT = next((p for p in Path(__file__).resolve().parents if p.name == "orch-dev"), Path.home() / "orch-dev")
PLAYWRIGHT = KIT / "e2e/dashboard/node_modules/playwright"
SHOTS = KIT / "state/e2e-shots"
pytestmark = pytest.mark.skipif(
    not (os.environ.get("ORCH_R5_BROWSER") and (DIST / "remote-client.html").is_file() and PLAYWRIGHT.is_dir()),
    reason="browser run: set ORCH_R5_BROWSER=1 with the UI built and Playwright installed",
)


@pytest.fixture(autouse=True)
def real_ui(monkeypatch):
    monkeypatch.setenv("ORCH_R5_STATIC", str(DIST))


def page_data(ws: str):
    def handler(method, p, body):
        feats = ["board", "tickets", "agents", "actions", "settings", "market", "remote", "worktrees"]
        row = {
            "id": ws, "prefix": "BILL", "name": OWN_NAME, "members": [{"person": "p_ann", "name": "Ann", "role": "owner"}],
            "gates": {g: {"approvers": "owner", "count": 1} for g in ("requirements", "plan", "verify", "code")}, "addons": {}, "features": feats,
            "counts": {}, "needs_you": 0, "host_state": "ok",
        }  # fmt: skip
        if p == "/api/session":
            return 200, dumps({"login": "remote-device", "features": feats, "workspaces": {ws: feats}})
        if p == f"/api/w/{ws}/today":
            return 200, dumps({"now": "2026-10-30T09:00:00Z", "workspace": ws, "needs_you": [], "read_only_open": [],
                               "waiting_on_others": {"count": 0, "people": []}, "working": [], "recent": [], "counts": {}})  # fmt: skip
        if p == f"/api/w/{ws}/tickets":
            return 200, dumps({"tickets": [], "items": []})
        if p == "/api/workspaces":
            return 200, dumps([row])
        if p.endswith("/remote/presence/nonce"):
            return 200, dumps({"nonce": b64u(b"n" * 32)})
        if p.endswith("/remote/presence"):
            handler.taken = True
            return 200, dumps({"ok": True, "n": 1})
        if p == f"/api/w/{ws}/agents/start":
            if not getattr(handler, "taken", False):
                tmpl = "/api/w/{ws}/agents/start"
                want = "sha256:" + P.route_digest(ws, "POST", tmpl, p, "", body).hex()
                err = {
                    "code": "remote.presence_required",
                    "message": "x",
                    "retryable": False,
                    "view_digest": want,
                    "template": tmpl,
                }
                return 428, dumps({"ok": False, "error": err})
            return 200, dumps({"started": True})
        return 200, dumps({})

    return handler


def test_the_real_pages_through_client_hub_and_host(stack, served, tmp_path):  # noqa: F811
    s, w = stack, stack.world
    opened_log, words_f, result_f = tmp_path / "opened.log", tmp_path / "words.json", tmp_path / "result.json"
    opened_log.write_text("")
    inner = s.ctl.launcher.opener

    def opener(uri):  # what the browser is given: the redirect file's path only
        with opened_log.open("a") as f:
            f.write(uri + "\n")
        return inner(uri)

    s.ctl.launcher.opener = opener
    s.ctl.open_home()
    home_uri = opened_log.read_text().split()[-1]
    owner = served.client()  # one connection: the host caps them
    code = ok(owner.req("remote.code", {}, idem=True))["data"]
    SHOTS.mkdir(parents=True, exist_ok=True)
    cfg = {
        "playwright": str(PLAYWRIGHT), "home": home_uri, "code": code["code"], "ws": w.ws, "ownName": OWN_NAME,
        "shots": str(SHOTS), "opened": str(opened_log), "words": str(words_f), "result": str(result_f),
    }  # fmt: skip
    r = Responder(w.dash, page_data(w.ws))
    r.start()
    node = shutil.which("node") or "node"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "TERM": "dumb",
        "PLAYWRIGHT_BROWSERS_PATH": str(KIT.parent / "Library/Caches/ms-playwright"),
    }
    proc = subprocess.Popen(
        [node, str(Path(__file__).with_name("remote_client_browser.mjs")), json.dumps(cfg)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )  # noqa: S603
    try:
        end = time.time() + 120
        confirmed = False
        while proc.poll() is None and time.time() < end:
            pend = ok(owner.req("remote.pending", {}, idem=True))["data"]["pending"]
            if pend and words_f.exists() and not confirmed:
                assert json.loads(words_f.read_text()) == pend[0]["words"].split()  # same six words at both ends
                from tests.host.human_helpers import human as _h  # noqa: F401

                s.world_human.run(
                    "remote.device_confirm", {"pending": pend[0]["pending"], "level_cap": "work", "days": 30}
                )
                confirmed = True
            time.sleep(0.2)
        out = proc.communicate(timeout=10)[0] if proc.poll() is not None else ""
    finally:
        if proc.poll() is None:
            proc.kill()  # our own child only
        r.stop = True
    assert proc.returncode == 0, out
    res = json.loads(result_f.read_text())
    assert res["status"] == 200, (res, out)
    assert confirmed
    print("browser log:", res["log"])
