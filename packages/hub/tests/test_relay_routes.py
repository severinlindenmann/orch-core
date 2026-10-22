"""D10: the relay routes' vectors (``phase4.relay_routes``), the sealed push payload and the real HTTP transport."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from channels_helpers import Env

from orch_hub.contract import hub as H
from orch_hub.contract import phase4 as P4
from orch_hub.contract import relayroutes as RR
from orch_hub.contract import suite as S
from orch_hub.contract.canon import cj
from orch_hub.relay import RelayClient

V = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub.json").read_text())
RRV = V["phase4"]["relay_routes"]
HUB = V["ids"]["hub"]


@pytest.mark.parametrize("name", ["push_put", "push_send", "mailbox_put"])
def test_request_bytes_are_the_signed_bytes_of_the_vector(name):
    e, rr = RRV[name], V["phase4"]["relay_request"]["request"]["headers"]
    msg = P4.relay_request_bytes(
        HUB,
        rr["Orch-Hub-Op-Cert"],
        int(rr["Orch-Hub-Ts"]),
        bytes.fromhex(rr["Orch-Hub-Nonce"]),
        e["method"],
        e["target"],
        bytes.fromhex(e["body_hex"]),
    )
    assert msg.hex() == e["signed_hex"]
    assert RR.route(e["method"], e["target"], HUB, bytes.fromhex(e["body_hex"]))["route"] == name


def test_push_seal_vector_opens_with_the_channel_key_and_nothing_else():
    s = RRV["push_seal"]
    priv = S.key_from_seed(bytes.fromhex(V["keys"][s["channel_key"]]["seed"]))
    args = {"hub_id": HUB, "channel_id": s["channel_id"], "msg_id": s["msg_id"]}
    got = RR.open_push(s["sealed"], channel_priv=priv, **args)
    assert got == s["payload"] and cj(got).hex() == s["payload_cj_hex"]
    assert RR._aad(HUB, s["msg_id"]).hex() == s["aad_hex"]
    again = RR.seal_push(
        s["payload"],
        channel_pub=S.public_bytes(priv),
        eph_seed=bytes.fromhex(s["eph_seed_hex"]),
        **args,
    )
    assert again == s["sealed"]
    for bad in (
        {"hub_id": "11" * 16},
        {"channel_id": "22" * 16},
        {"msg_id": "33" * 16},
    ):
        with pytest.raises(H.Refused):
            RR.open_push(s["sealed"], channel_priv=priv, **(args | bad))


def test_seal_refuses_a_channel_id_that_is_not_the_keys():
    priv = S.key_from_seed(bytes(range(32)))
    with pytest.raises(H.Refused):
        RR.seal_push(
            RR.push_payload("prepare", 1, None),
            hub_id=HUB,
            channel_id="aa" * 16,
            msg_id="bb" * 16,
            channel_pub=S.public_bytes(priv),
            eph_seed=bytes(32),
        )


def test_payload_types_are_a_closed_list():
    for bad in ("approve", "", None):
        with pytest.raises(H.Refused):
            RR.push_payload(bad, 1, None)
    with pytest.raises(H.Refused):
        RR.push_payload("prepare", 1, "not-hex")


def test_default_transport_over_real_http_on_loopback(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.command, self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"hub_id":"x","created":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    env = Env(tmp_path)
    try:
        c = RelayClient(env.keys, f"http://127.0.0.1:{srv.server_address[1]}", now_ms=env.clock)
        assert c.register()["created"] is True
        assert seen == [("POST", "/v2/hubs", cj(env.keys.card()))]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
        env.close()


def test_cli_relay_register_prints_the_answer_and_refuses_without_a_relay(tmp_path, capsys, monkeypatch):
    from orch_hub.cli import main
    from orch_hub.keys import HubKeys

    state = tmp_path / "hub"
    HubKeys.init(state)  # no relay_url in the card
    assert main(["relay", "--state", str(state), "register"]) == 1
    assert "no_relay" in capsys.readouterr().err
    monkeypatch.setattr(RelayClient, "register", lambda self: {"hub_id": self.hub_id, "created": True})
    assert main(["relay", "--state", str(state), "--relay-url", "http://127.0.0.1:9", "register"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is True
