"""Hub one-app, two real origins (H1, 27 Oct): the real hub web API on one port and the real dashboard server (its own
venv, subprocess) on another. The dashboard's host link is a bridge to the hub's real link-op handlers, so every HTTP
rule on both sides is the shipped code; only the TLS link and the core host op are replaced (the host op is tested in
core, tests/host/test_hub_switch.py).

Switch both ways lands signed in on the deep link; a used ticket is refused; an expired one is refused; a sign-out
ticket signs out the other app; a ticket never replaces another person's session silently."""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from webapi_helpers import WS, Web

from orch_hub.contract.hub import Refused
from orch_hub.link import ops
from orch_hub.switch import SwitchService
from orch_hub.webapi import WebServer

DASH_PY = Path(__file__).resolve().parents[2] / "addons/dashboard/server/.venv/bin/python"
DRIVER = Path(__file__).parent / "e2e_support/dashboard_driver.py"
pytestmark = pytest.mark.skipif(not DASH_PY.exists(), reason="the dashboard server venv is not set up (uv sync there)")


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """A browser by hand: one origin, a cookie jar of one cookie, JSON in and out."""

    def __init__(self, port: int) -> None:
        self.port, self.cookie, self.bearer = port, None, None
        self.origin = f"http://127.0.0.1:{port}"

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        h = {"Origin": self.origin, "x-orch-csrf": "1", **(headers or {})}
        if self.cookie:
            h["Cookie"] = self.cookie
        if self.bearer:
            h["Authorization"] = "Bearer " + self.bearer
        raw = None if body is None else json.dumps(body)
        if raw is not None:
            h["content-type"] = "application/json"
        c.request(method, path, raw, h)
        r = c.getresponse()
        data = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            pair = sc.split(";")[0]
            self.cookie = None if sc.split(";")[0].endswith("=") or "Max-Age=0" in sc else pair
        c.close()
        try:
            return r.status, json.loads(data) if data else None
        except ValueError:
            return r.status, data


@pytest.fixture
def stack(tmp_path):
    yield from build_stack(tmp_path)


def build_stack(tmp_path, static_dir=None):
    """The hub and the real dashboard on two ports. ``static_dir``: the built single-page app to serve (default: a stub)."""
    w = Web(tmp_path)
    clock = [1_000_000]
    hub_port, dash_port = free_port(), free_port()
    hub_origin, dash_origin = f"http://127.0.0.1:{hub_port}", f"http://127.0.0.1:{dash_port}"
    w.sw = SwitchService(hub_origin=lambda: hub_origin, hub_name="Acme", now_ms=lambda: clock[0])
    w.api.d.switch = w.sw
    w.api.allow_origin(hub_origin)
    w.reg.ws[WS]["owner"] = w.ann

    class Bridge(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            args = json.loads(self.rfile.read(int(self.headers["content-length"])))
            kind = args.pop("kind")
            op = {"ticket": "switch_ticket", "redeem": "switch_redeem", "rail": "rail_get"}[kind]
            body = {"ticket": "ticket", "ticket_kind": "kind"}  # the host op's names -> the link op's
            args = {body.get(k, k): v for k, v in args.items()}
            ctx = ops.OpContext(
                hub_id="h", workspace_id=WS, link_wsk_pub=b"", body=args, registry=w.reg, invites=None, snapshots=None,
                publish=None, downstream=None, now_ms=0, dev_mode=True, access=w.env.acc, switch=w.sw,
            )  # fmt: skip
            try:
                out, code = getattr(ops, op)(ctx), 200
            except Refused as e:
                out, code = {"error": "hub.refused" if e.code else e.code}, 400
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    bridge = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    hub = WebServer(w.api, "127.0.0.1", hub_port)
    hub.start()
    state = tmp_path / "dash"
    (state / "assets").mkdir(parents=True)
    (state / "index.html").write_text("<html></html>")
    proc = subprocess.Popen(
        [str(DASH_PY), str(DRIVER), str(dash_port), f"http://127.0.0.1:{bridge.server_address[1]}", WS, str(state), *([str(static_dir)] if static_dir else [])],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )  # fmt: skip
    line = proc.stdout.readline()
    assert line.startswith("READY "), line
    code = line.split()[1]
    dash = Browser(dash_port)
    s, o = dash.call("POST", "/auth/session", {"code": code})
    assert s == 200, o
    dash.bearer = o["token"]
    yield types_ns(w=w, clock=clock, hub=Browser(hub_port), dash=dash, dash_port=dash_port, hub_origin=hub_origin,
                   dash_origin=dash_origin, state=state, proc=proc)  # fmt: skip
    proc.stdin.close()
    proc.wait(10)
    hub.close()
    bridge.shutdown()
    w.close()


def types_ns(**kw):
    import types

    return types.SimpleNamespace(**kw)


def login_hub(st, user):
    s, o = st.hub.call(
        "POST", "/api/v1/session",
        {"user_id": user, "password": "correct horse battery", "totp": st.w.env.next_code(user), "client": "browser"},
    )  # fmt: skip
    assert s == 200, o


def ticket_of(url):
    return re.search(r"#t=([0-9a-f.]+)$", url).group(1)


def test_hub_to_workspace_lands_signed_in_once(stack):
    st = stack
    login_hub(st, st.w.ann)
    # the dashboard tells the hub where it lives the first time it asks for the rail
    s, rail = st.dash.call("GET", f"/api/w/{WS}/hub/rail")
    assert s == 200 and rail["hub"]["name"] == "Acme" and rail["workspaces"][0]["current"] is True
    s, o = st.hub.call("POST", "/api/v1/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "open"})
    assert s == 200 and o["url"].startswith(st.dash_origin + "/#t=")
    t = ticket_of(o["url"])
    fresh = Browser(st.dash_port)  # a second browser with no session at the dashboard
    s, got = fresh.call("POST", "/auth/switch", {"t": t})
    assert s == 200 and got["return_to"] == "/" and got["workspace_id"] == WS
    fresh.bearer = got["token"]
    assert fresh.call("GET", f"/api/w/{WS}/hub/rail")[0] == 200  # signed in
    s, denied = fresh.call("POST", f"/api/w/{WS}/settings", {})  # a switch session reads; it changes nothing
    assert s == 403 and denied["error"]["code"] == "step_up_required"
    assert fresh.call("GET", f"/api/w/{'c' * 32}/head")[0] == 403
    assert Browser(st.dash_port).call("POST", "/auth/switch", {"t": t})[0] == 401  # one use


def test_expired_and_foreign_tickets_are_refused_at_the_workspace(stack):
    st = stack
    login_hub(st, st.w.ann)
    st.dash.call("GET", f"/api/w/{WS}/hub/rail")
    _, o = st.hub.call("POST", "/api/v1/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "open"})
    st.clock[0] += 31_000
    assert Browser(st.dash_port).call("POST", "/auth/switch", {"t": ticket_of(o["url"])})[0] == 401
    assert Browser(st.dash_port).call("POST", "/auth/switch", {"t": "b" * 32 + "." + "c" * 32})[0] == 401


def test_workspace_to_hub_confirm_then_signed_in_deep_link_once(stack):
    st = stack
    s, o = st.dash.call("POST", f"/api/w/{WS}/hub/switch", {"return_to": "/", "kind": "open"})
    assert s == 200 and o["url"].startswith(st.hub_origin + "/#t=")
    t = ticket_of(o["url"])
    hub = st.hub  # a browser with no hub session
    s, peek = hub.call("POST", "/api/v1/switch/peek", {"t": t})
    assert s == 200 and peek["user_id"] == st.w.ann and peek["signed_in_as"] is None
    s, got = hub.call("POST", "/api/v1/switch/redeem", {"t": t})
    assert s == 200 and got["kind"] == "open"
    s, me = hub.call("GET", "/api/v1/session")
    assert s == 200 and me["user_id"] == st.w.ann and me["needs_sign_in"] is True
    assert hub.call("GET", "/api/v1/admin/users")[1]["error"] == "reauth_required"  # read-only until a real sign-in
    assert Browser(hub.port).call("POST", "/api/v1/switch/redeem", {"t": t})[0] == 403  # one use


def test_a_ticket_does_not_replace_another_persons_hub_session(stack):
    st = stack
    login_hub(st, st.w.admin)
    s, o = st.dash.call("POST", f"/api/w/{WS}/hub/switch", {"return_to": "/", "kind": "open"})
    t = ticket_of(o["url"])
    s, e = st.hub.call("POST", "/api/v1/switch/redeem", {"t": t})
    assert (s, e["error"]) == (403, "other_user")
    assert st.hub.call("GET", "/api/v1/session")[1]["user_id"] == st.w.admin  # still the admin
    assert st.hub.call("POST", "/api/v1/switch/redeem", {"t": t, "replace": True})[0] == 200


def test_sign_out_of_the_other_app_both_ways(stack):
    st = stack
    login_hub(st, st.w.ann)
    st.dash.call("GET", f"/api/w/{WS}/hub/rail")
    # workspace asks to sign out of the hub: the hub session ends
    s, o = st.dash.call("POST", f"/api/w/{WS}/hub/switch", {"return_to": "/", "kind": "signout"})
    s, got = st.hub.call("POST", "/api/v1/switch/redeem", {"t": ticket_of(o["url"])})
    assert s == 200 and got["kind"] == "signout"
    assert st.hub.call("GET", "/api/v1/session")[0] == 401
    # hub asks to sign out of the workspace: nothing is opened, the page ends its own session
    login_hub(st, st.w.ann)
    s, o = st.hub.call("POST", "/api/v1/switch/ticket", {"workspace_id": WS, "return_to": "/", "kind": "signout"})
    s, got = Browser(st.dash_port).call("POST", "/auth/switch", {"t": ticket_of(o["url"])})
    assert (s, got) == (200, {"signed_out": True})
