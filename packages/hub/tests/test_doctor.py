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
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True).start()
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
    srv = LinkServer.from_keys(keys, registry=reg, invites=FixedInvites([]), dev_mode=True).start()
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


def test_doctor_is_read_only_a_stray_generation_survives(state, capsys):
    stray = state / "op" / "99"
    stray.mkdir(mode=0o700)
    (stray / "key").write_bytes(b"x")
    os.chmod(stray / "key", 0o600)
    doctor(capsys, state)
    assert stray.is_dir() and (stray / "key").exists()
    HubKeys.open(state)  # the default open still prunes uncommitted generations
    assert not stray.exists()


# ---- §4a co-location checks (D12) -------------------------------------------------------------------------------

from orch_hub.doctor import Accounts, run


def _acc(extra=None, *, euser="orch-hub", groups=None):
    users = {
        "orch-hub": (986, 980),
        "orch-relay": (995, 987),
        "orch-publish": (993, 985),
        "orch-host-ws2": (992, 984),
        "agent-ws2": (991, 983),
    }
    users.update(extra or {})
    return Accounts(users, groups or {}, users[euser][0] if euser in users else 1234, euser)


def _fw(tmp_path, ids='0, "orch-hub", "orch-host-ws2"'):
    f = tmp_path / "hub-fw.nft"
    f.write_text(
        f'table inet orch_hub {{ chain out {{ oifname "lo" tcp dport 8105 meta skuid != {{ {ids} }} reject }} }}'
    )
    return f


def _run(state, acc, fw, capsys, listen=None):
    import io

    out = io.StringIO()
    run(state, listen=listen, out=out, accounts=acc, fw_file=fw)
    return out.getvalue()


def test_colocation_good_layout_passes_and_is_reported(state, tmp_path, capsys):
    out = _run(state, _acc(), _fw(tmp_path), capsys)
    assert "INFO layout: one machine (hub + 1 workspace host(s), relay/publish)" in out
    assert "weaker isolation" in out
    for name in ("hub runs as its own OS user", "host and agent users are separate", "link firewall allows only"):
        assert f"PASS {name}" in out, out


def test_split_layout_has_no_colocation_checks(state, capsys):
    acc = Accounts({"orch-hub": (986, 980)}, {}, 986, "orch-hub")
    out = _run(state, acc, DEFAULT_FW_MISSING, capsys)
    assert "INFO layout: hub alone" in out
    assert "link firewall" not in out


DEFAULT_FW_MISSING = __import__("pathlib").Path("/nonexistent/hub-fw.nft")


@pytest.mark.parametrize(
    "acc,needle",
    [
        (_acc(euser="orch-host-ws2"), "runs as orch-host-ws2"),
        (_acc({"agent-x": (986, 777)}), "orch-hub uid or primary group shared with agent-x"),
        (_acc(groups={980: frozenset({"agent-ws2"})}), "group orch-hub has other members: agent-ws2"),
    ],
)
def test_colocation_hub_user_failures(state, tmp_path, capsys, acc, needle):
    out = _run(state, acc, _fw(tmp_path), capsys)
    assert f"FAIL hub runs as its own OS user: {needle}" in out, out


def test_root_hub_fails(state, tmp_path, capsys):
    acc = Accounts(_acc().users, {}, 0, "root")
    assert "FAIL hub runs as its own OS user: hub tooling runs as root" in _run(state, acc, _fw(tmp_path), capsys)


def test_host_or_agent_sharing_identity_fails(state, tmp_path, capsys):
    out = _run(state, _acc({"orch-host-ws3": (992, 900)}), _fw(tmp_path), capsys)
    assert "orch-host-ws3 and orch-host-ws2 share a uid" in out
    out = _run(state, _acc({"agent-ws3": (986, 5)}), _fw(tmp_path), capsys)
    assert "agent user agent-ws3 shares an identity" in out
    out = _run(state, _acc(groups={980: frozenset({"orch-hub", "agent-ws2"})}), _fw(tmp_path), capsys)
    assert "FAIL host and agent users are separate from the hub: agent user agent-ws2 is in group orch-hub" in out


@pytest.mark.parametrize(
    "ids,needle",
    [
        ('0, "orch-hub", "orch-host-ws2", "agent-ws2"', "not root, orch-hub or an orch-host-* user"),
        ("0, 991, 986, 992", "not root, orch-hub or an orch-host-* user"),
        ('0, "orch-host-ws2"', "does not allow the orch-hub uid"),
        ('0, "orch-hub", "nobody-here"', "unknown user"),
    ],
)
def test_link_firewall_failures(state, tmp_path, capsys, ids, needle):
    out = _run(state, _acc(), _fw(tmp_path, ids), capsys)
    assert "FAIL link firewall allows only root, hub and host users" in out
    assert needle in out, out


def test_link_firewall_missing_or_ruleless_fails(state, tmp_path, capsys):
    assert "missing or unreadable" in _run(state, _acc(), tmp_path / "nope", capsys)
    f = tmp_path / "x.nft"
    f.write_text("table inet orch_hub {}")
    assert "no 'meta skuid" in _run(state, _acc(), f, capsys)
