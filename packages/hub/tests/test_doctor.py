"""``orch-hub doctor``: PASS/FAIL lines, exit code, no secrets in output."""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orch_hub.cli import main
from orch_hub.keys import HubKeys
from orch_hub.link import LinkServer, Registry
from orch_hub.serve import REGISTRY_FILE, FixedInvites


@pytest.fixture
def state(tmp_path):
    s = tmp_path / "hub"
    HubKeys.init(s)
    return s


@pytest.fixture
def publish():
    box = {"status": 400, "body": b'{"error":"malformed"}'}

    class H(BaseHTTPRequestHandler):
        def do_PUT(self):
            self.rfile.read(int(self.headers.get("content-length") or 0))
            self.send_response(box["status"])
            self.send_header("content-length", str(len(box["body"])))
            self.end_headers()
            self.wfile.write(box["body"])

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    box["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield box
    srv.shutdown()
    srv.server_close()


def doctor(capsys, state, *extra):
    rc = main(["doctor", "--state", str(state), *extra])
    return rc, capsys.readouterr().out


def test_all_pass_with_listen_and_publish(state, publish, capsys):
    keys = HubKeys.open(state)
    reg = Registry(str(state / REGISTRY_FILE))
    os.chmod(state / REGISTRY_FILE, 0o600)
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([])).start()
    try:
        rc, out = doctor(capsys, state, "--listen", f"127.0.0.1:{srv.address[1]}", "--publish-url", publish["url"])
    finally:
        srv.close()
        reg.close()
    assert rc == 0, out
    assert "FAIL" not in out
    assert out.count("PASS ") == 6
    assert "doctor: all checks passed" in out


def test_loose_state_dir_fails(state, capsys):
    os.chmod(state, 0o755)
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "FAIL state dir and key file modes" in out
    assert "FAIL key store opens" in out


def test_loose_key_file_fails_and_no_key_bytes_in_output(state, capsys):
    key = state / "hk_root.key"
    secret = key.read_bytes().hex()
    os.chmod(key, 0o644)
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "hk_root.key must be 0600" in out
    assert secret not in out


def test_missing_state_fails(tmp_path, capsys):
    rc, out = doctor(capsys, tmp_path / "nope")
    assert rc == 1
    assert "FAIL key store opens" in out


def test_non_dev_card_fails(state, capsys, monkeypatch):
    monkeypatch.setattr(HubKeys, "verify_card", lambda self, c: {"card": dict(c["o"], dev=False)})
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "FAIL card verifies and is dev: true: card is not dev: true" in out


def test_tls_pin_mismatch_fails(state, capsys):
    keys = HubKeys.open(state)
    reg = Registry()
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([])).start()
    try:
        keys.reissue_card(tls_spki_sha256="A" * 43)  # the card now names another key than the server presents
        rc, out = doctor(capsys, state, "--listen", f"127.0.0.1:{srv.address[1]}")
    finally:
        srv.close()
        reg.close()
    assert rc == 1
    assert "FAIL TLS pin matches the served key: served TLS key does not match" in out


def test_non_loopback_listen_is_refused(state, capsys):
    rc, out = doctor(capsys, state, "--listen", "0.0.0.0:1")
    assert rc == 1
    assert "FAIL TLS pin" in out


@pytest.mark.parametrize(
    "status,body",
    [(200, b'{"ok":true}'), (404, b"{}"), (400, b'{"error":"bad_sig"}'), (400, b"not json")],
)
def test_publish_route_wrong_answer_fails(state, publish, capsys, status, body):
    publish.update(status=status, body=body)
    rc, out = doctor(capsys, state, "--publish-url", publish["url"])
    assert rc == 1
    assert "FAIL publish URL answers /v2/hub/card" in out


def test_publish_unreachable_fails(state, capsys):
    rc, out = doctor(capsys, state, "--publish-url", "http://127.0.0.1:1")
    assert rc == 1
    assert "publish URL not reachable" in out


def test_unreadable_registry_fails(state, capsys):
    (state / REGISTRY_FILE).write_bytes(b"not sqlite")
    os.chmod(state / REGISTRY_FILE, 0o600)
    rc, out = doctor(capsys, state)
    assert rc == 1
    assert "FAIL registry readable" in out
