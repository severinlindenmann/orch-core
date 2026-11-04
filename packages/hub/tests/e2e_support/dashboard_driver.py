"""Runs the REAL dashboard server (orch_dashboard.create_app, its Security layer, auth and routes) on 127.0.0.1 for the
two-origin switch test, in the dashboard's own venv. Its host link is a bridge: `hub.switch` goes over HTTP to the test,
which answers with the hub's real link-op handlers. Usage: dashboard_driver.py PORT BRIDGE_URL WORKSPACE_ID STATEDIR [STATIC_DIR].
Prints `READY <launch code>` once it listens."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from orch_dashboard.app import create_app
from orch_dashboard.auth import Auth
from orch_dashboard.errors import fail
from orch_dashboard.gateway import Gateway

port, bridge, ws, state = int(sys.argv[1]), sys.argv[2], sys.argv[3], Path(sys.argv[4])
static = Path(sys.argv[5]) if len(sys.argv) > 5 else state
PERSON = "p_owner1"  # this computer's user in the workspace: its owner (the hub's owner alias is the ticket's user)


class Link:
    welcome = {"peer": {"person": PERSON}}  # noqa: RUF012

    def supports(self, op, cap="ops/1"):
        return op == "hub.switch"

    def call(self, op, args=None, **kw):
        if op == "member.list":
            return {"members": [{"person": PERSON, "name": "Ann", "role": "owner", "devices": []}]}
        if op == "workspace.show":
            return {"name": "Product", "prefix": "PRD"}
        if op != "hub.switch":
            raise fail("not_found")
        req = urllib.request.Request(
            bridge, data=json.dumps(args).encode(), headers={"content-type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            code = json.loads(e.read()).get("error", "hub.refused")
            raise fail(code) from None


class Gw(Gateway):
    def people(self):
        return {ws: PERSON}

    def enabled(self, w):
        if w != ws:
            raise fail("not_found")
        return Link()


auth = Auth(trust_file=state / "trusted.json")
app = create_app(port, Gw(state), auth, static)
config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)
print("READY " + auth.launch(port)["code"], flush=True)
for line in sys.stdin:  # runs until the test closes our stdin; `launch` prints a fresh launch code (30 s)
    if line.strip() == "launch":
        print("CODE " + auth.launch(port)["code"], flush=True)
