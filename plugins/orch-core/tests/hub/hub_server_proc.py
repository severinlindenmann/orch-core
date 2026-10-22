"""Run by the cross-repo test under orch-hub's own environment (``ORCH_HUB_SRC``): starts the real hub link server
from a fresh key store, prints one JSON line with what a host needs, then serves until stdin closes. Not a test."""

import json
import sys
import tempfile
from pathlib import Path

hub_src = Path(sys.argv[1])
sys.path[:0] = [str(hub_src / "src"), str(hub_src / "tests")]

from orch_hub.contract import hub as H  # noqa: E402
from orch_hub.contract import vectorgen as G  # noqa: E402
from orch_hub.contract.canon import b64u  # noqa: E402
from orch_hub.keys import HubKeys  # noqa: E402
from orch_hub.link import LinkServer, Registry  # noqa: E402
from orch_hub.link.tls import TlsIdentity  # noqa: E402

from conftest import Invites  # noqa: E402

with tempfile.TemporaryDirectory() as d:
    keys = HubKeys.init(Path(d) / "hub")
    ident = TlsIdentity.generate()
    reg = Registry()
    srv = LinkServer.from_keys(keys, identity=ident, registry=reg, invites=Invites(inv=None)).start()
    card = keys.card()
    H.verify_card(card, now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)
    o = {**G.binding(ws=G.WS_A)["o"], "hub_id": keys.hub_id, "hk_root_pub": b64u(keys.hk_root_pub)}
    print(
        json.dumps(
            {
                "port": srv.address[1],
                "spki": card["o"]["tls_spki_sha256"],
                "hub_id": keys.hub_id,
                "hk_op_pub": keys.op_cert()["o"]["hk_op_pub"],
                "workspace_id": G.WS_A,
                "wsk_scalar": G.K.priv["wsk_a"].private_numbers().private_value,
                "join": {
                    "invite": "inv",
                    "card": G.v2_card(),
                    "pk_pub": b64u(G.K.pub["pk_alice"]),
                    "binding": G.signed("wsk_a", "sig_hub_binding", o),
                },
                "op_cert_id": keys.op_cert()["o"]["cert_id"],
            }
        ),
        flush=True,
    )
    sys.stdin.read()
    srv.close()
    reg.close()
