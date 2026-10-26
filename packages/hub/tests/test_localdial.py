"""A computer cannot dial its own overlay address: its own IPv4 addresses are dialled on loopback."""

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from orch_hub.localdial import dial_host, dialling_locally


def test_dial_host_rewrites_only_own_addresses():
    assert dial_host("203.0.113.9") == "203.0.113.9" and dial_host("example.com") == "example.com"
    assert dial_host("127.0.0.1") == "127.0.0.1"
    s = socket.socket()
    s.close()
    dial_host.cache_clear()


def test_connection_dials_loopback_but_keeps_the_host_header(monkeypatch):
    import orch_hub.localdial as L

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers["Host"]
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    monkeypatch.setattr(L, "dial_host", lambda h: "127.0.0.1" if h == "100.71.14.98" else h)
    c = dialling_locally(http.client.HTTPConnection("100.71.14.98", port, timeout=3))
    c.request("GET", "/")
    assert c.getresponse().status == 200
    assert seen["host"] == f"100.71.14.98:{port}"
    srv.shutdown()
