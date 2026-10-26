"""``orch-hub serve`` as a real process: ready line, join + publish_out over the real link, SIGTERM, restart restores
the persisted binding and re-pushes it to orch-publish (a loopback double here)."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import time

import pytest
from publish_helpers import HOST, FakeUpstream, binding, host_request

from orch_hub import cli
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.keys import HubKeys
from orch_hub.link import LinkClient

READY = re.compile(r"orch-hub ready port=(\d+) tls_spki_sha256=([A-Za-z0-9_-]{43}) hub_id=([0-9a-f]{32})\n")


@pytest.fixture
def up():
    u = FakeUpstream()
    yield u
    u.close()


def start(state, up, *invites):
    args = [sys.executable, "-m", "orch_hub.cli", "serve", "--state", str(state), "--listen", "127.0.0.1:0"]
    args += ["--publish-url", up.url, "--api-host", HOST]
    for i in invites:
        args += ["--invite", i]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert p.stdout
    m = READY.fullmatch(p.stdout.readline())
    assert m, p.stderr.read() if p.poll() is not None and p.stderr else "no ready line"
    return p, int(m[1]), m[2], m[3]


def stop(p):
    p.send_signal(signal.SIGTERM)  # our own child
    assert p.wait(10) == 0


def link(port, pin, hub_id, hub_pub, first):
    return LinkClient.open(
        "127.0.0.1",
        port,
        tls_spki_sha256=pin,
        hub_id=hub_id,
        workspace_id=G.WS_A,
        wsk=KS.WskSigner(G.K.priv["wsk_a"]),
        hk_op_pub=hub_pub,
        send_wsk_pub=first,
        timeout=5,
    )


def test_serve_join_publish_restart(tmp_path, up):
    keys = HubKeys.init(tmp_path / "hub")
    p, port, pin, hub_id = start(tmp_path / "hub", up, "inv1")
    try:
        assert hub_id == keys.hub_id
        keys = HubKeys.open(tmp_path / "hub")  # the card was re-issued with the TLS pin
        assert keys.card()["o"]["tls_spki_sha256"] == pin
        hub_pub = keys.op_signer.public
        c = link(port, pin, hub_id, hub_pub, True)
        body = {
            "invite": "inv1",
            "card": G.v2_card(),
            "pk_pub": b64u(G.K.pub["pk_alice"]),
            "binding": binding(G.K.priv["wsk_a"], keys, G.WS_A),
        }
        assert c.request("join", body) == {}
        req = host_request(G.K.priv["wsk_a"], hub_id, G.WS_A)
        assert c.request("publish_out", {"request": req})["status"] == 200
        seen = [(r["method"], r["target"]) for r in up.requests]
        assert seen[0] == ("PUT", f"/v2/hub/cards/{G.WS_A}")  # the join's card goes to publish first (R5a)
        assert ("PUT", "/v2/hub/card") in seen
        assert ("PUT", f"/v2/hub/bindings/{G.WS_A}") in seen and seen[-1][0] == "GET"
        c.close()
    finally:
        stop(p)
    assert (tmp_path / "hub" / "registry.db").stat().st_mode & 0o077 == 0

    # restart on the same state: no join needed, the binding comes back from the registry
    n = len(up.requests)
    p, port, pin, hub_id = start(tmp_path / "hub", up)
    try:
        time.sleep(0.3)
        c = link(port, pin, hub_id, hub_pub, False)
        assert c.request("publish_out", {"request": host_request(G.K.priv["wsk_a"], hub_id, G.WS_A)})["status"] == 200
        c.close()
        again = [(r["method"], r["target"]) for r in up.requests[n:]]
        assert ("PUT", "/v2/hub/card") in again and ("PUT", f"/v2/hub/bindings/{G.WS_A}") in again
        assert json.loads(up.requests[n]["body"])  # the pushed objects are the stored signed ones
    finally:
        stop(p)


@pytest.mark.parametrize("listen", ["0.0.0.0:0", "192.0.2.1:80", "127.0.0.1", "127.0.0.1:x", "example.com:80"])
def test_serve_is_loopback_only(tmp_path, capsys, listen):
    HubKeys.init(tmp_path / "hub")
    rc = cli.main(
        ["serve", "--state", str(tmp_path / "hub"), "--listen", listen, "--publish-url", "http://127.0.0.1:1"]
    )
    assert rc == 1 and "loopback" in capsys.readouterr().err


def test_serve_without_keys_is_refused(tmp_path, capsys):
    rc = cli.main(
        ["serve", "--state", str(tmp_path / "none"), "--listen", "127.0.0.1:0", "--publish-url", "http://127.0.0.1:1"]
    )
    assert rc == 1 and "refused" in capsys.readouterr().err


def test_serve_prints_invites_as_hub_id_colon_code(tmp_path, up):
    """Protocol §5: every invite is shown as `invite <hub_id>:<code>`; the `=USER_ID` alias never appears."""
    user = "ab" * 16
    HubKeys.init(tmp_path / "hub")
    p, _port, _pin, hub_id = start(tmp_path / "hub", up, "inv1", f"inv2={user}")
    try:
        assert p.stdout
        lines = sorted(p.stdout.readline().rstrip("\n") for _ in range(2))
        assert lines == [f"invite {hub_id}:inv1", f"invite {hub_id}:inv2"]
    finally:
        stop(p)
