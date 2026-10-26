"""A computer cannot dial its own overlay address: its own IPv4 addresses are dialled on loopback."""

import http.client
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from orch.hubclient import connectlink
from orch.wsproto import localdial as L


def test_dial_host_rewrites_only_own_addresses():
    assert L.dial_host("203.0.113.9") == "203.0.113.9"
    assert L.dial_host("example.com") == "example.com"
    assert L.dial_host("127.0.0.1") == "127.0.0.1"


def test_local_url_keeps_the_host_header(monkeypatch):
    monkeypatch.setattr(L, "dial_host", lambda h: "127.0.0.1" if h == "100.71.14.98" else h)
    assert L.local_url("http://100.71.14.98:8100/api/v1/hub/card") == (
        "http://127.0.0.1:8100/api/v1/hub/card",
        "100.71.14.98:8100",
    )
    assert L.local_url("http://example.org/x") == ("http://example.org/x", None)


def test_connections_and_card_fetch_dial_loopback_with_the_original_host(monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers["Host"])
            body = b'{"card": {"x": 1}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(L, "dial_host", lambda h: "127.0.0.1" if h == "100.71.14.98" else h)
    c = L.dialling_locally(http.client.HTTPConnection("100.71.14.98", port, timeout=3))
    c.request("GET", "/")
    assert c.getresponse().status == 200
    link = connectlink.Link(origin=f"http://100.71.14.98:{port}", hub_id="0" * 32, code="c", endpoint=None)
    assert connectlink.fetch_card(link) == {"x": 1}
    assert seen == [f"100.71.14.98:{port}"] * 2
    srv.shutdown()
