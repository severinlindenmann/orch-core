"""Run-9 feedback 6: the workspace connect link. ``hub.link`` fetches the card the link names and checks it against the
link's hub id; nothing is signed or kept."""

from __future__ import annotations

import copy
import http.server
import json
import threading

import pytest

from orch.host import hubops
from orch.hubclient import connectlink
from orch.hubclient.errors import HubRefused
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err
from tests.hub.vec import V

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
CARD = V["objects"]["card"]
HUB_ID = V["ids"]["hub"]


@pytest.fixture(autouse=True)
def dev_workspace(monkeypatch):
    monkeypatch.setattr(hubops, "workspace_dev", lambda service: True)


@pytest.fixture
def hub_card_server():
    """A tiny http server that answers GET /api/v1/hub/card like the hub console does."""
    box = {"card": copy.deepcopy(CARD)}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/v1/hub/card":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"card": box["card"], "api_origin": "http://x"}).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    box["origin"] = f"http://127.0.0.1:{srv.server_port}"
    yield box
    srv.shutdown()


def test_the_link_format_vector():
    link = connectlink.parse_link(
        "https://hub.example.test:8443/c#h1.0123456789abcdef0123456789abcdef.Ab3_x-9Zq.10.0.0.5:8105"
    )
    assert (link.origin, link.hub_id, link.code, link.endpoint) == (
        "https://hub.example.test:8443", "0123456789abcdef0123456789abcdef", "Ab3_x-9Zq", "10.0.0.5:8105",
    )  # fmt: skip
    assert link.card_url == "https://hub.example.test:8443/api/v1/hub/card"
    assert connectlink.parse_link("http://h/c#h1." + "a" * 32 + ".abc").endpoint is None


@pytest.mark.parametrize(
    "bad",
    ["http://h/x#h1." + "a" * 32 + ".abc", "http://h/c#h2." + "a" * 32 + ".abc", "http://h/c#h1.abc.abc",
     "http://h/c?q=1#h1." + "a" * 32 + ".abc", "ftp://h/c#h1." + "a" * 32 + ".abc",
     "http://h/c#h1." + "a" * 32 + ".abc.nope"],
)  # fmt: skip
def test_a_bad_link_is_refused(bad):
    with pytest.raises(HubRefused):
        connectlink.parse_link(bad)


def test_the_op_returns_the_verified_card(human, served, hub_card_server):
    link = f"{hub_card_server['origin']}/c#h1.{HUB_ID}.Code_1.127.0.0.1:8105"
    got = ok(served.client().req("hub.link", {"link": link}))["data"]
    assert got["hub_id"] == HUB_ID and got["code"] == "Code_1" and got["endpoint"] == "127.0.0.1:8105"
    assert got["hub_card"] == CARD and got["name"].startswith("127.0.0.1:") and got["dev"] is True


def test_a_card_of_another_hub_is_refused(human, served, hub_card_server):
    link = f"{hub_card_server['origin']}/c#h1.{'0' * 32}.Code_1"
    assert err(served.client().req("hub.link", {"link": link})) != ""
    assert hub_card_server["card"] == CARD
