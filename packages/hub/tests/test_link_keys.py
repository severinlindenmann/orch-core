"""The link server started from a real HubKeys state dir: HK_op signs the replies, the card pins the TLS key."""

from __future__ import annotations

from conftest import Invites

from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, unb64u
from orch_hub.keys import HubKeys
from orch_hub.link import LinkClient, LinkServer, Registry
from orch_hub.link.tls import TlsIdentity


def test_server_from_real_hubkeys(tmp_path):
    keys = HubKeys.init(tmp_path / "hub")
    ident = TlsIdentity.generate()
    reg = Registry()
    srv = LinkServer.from_keys(keys, identity=ident, registry=reg, invites=Invites(inv=None), dev_mode=True).start()
    c = None
    try:
        card = keys.card()  # re-issued with the real SPKI pin
        H.verify_card(card, now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)
        assert card["o"]["tls_spki_sha256"] == ident.spki_sha256
        hk_op = unb64u(keys.op_cert()["o"]["hk_op_pub"], 65)
        ws = G.WS_A
        # a binding for THIS hub (the vector hub is another one)
        o = {**G.binding(ws=ws)["o"], "hub_id": keys.hub_id, "hk_root_pub": b64u(keys.hk_root_pub)}
        binding = G.signed("wsk_a", "sig_hub_binding", o)
        c = LinkClient.open(
            "127.0.0.1",
            srv.address[1],
            tls_spki_sha256=card["o"]["tls_spki_sha256"],
            hub_id=keys.hub_id,
            workspace_id=ws,
            wsk=KS.WskSigner(G.K.priv["wsk_a"]),
            hk_op_pub=hk_op,
            send_wsk_pub=True,
            now_ms=keys._now,
        )
        body = {
            "invite": "inv",
            "card": G.v2_card(),
            "pk_pub": b64u(G.K.pub["pk_alice"]),
            "binding": binding,
        }
        assert c.request("join", body) == {}
        snap = c.request("snapshot_get", {})["snapshot"]
        assert snap["o"]["hub_id"] == keys.hub_id and snap["o"]["op_cert_id"] == keys.op_cert()["o"]["cert_id"]
    finally:
        if c:
            c.close()
        srv.close()
        reg.close()
