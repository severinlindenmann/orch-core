"""Run by the cross-repo route test under orch-hub's own environment (``ORCH_HUB_SRC``): the real link server WITH its
router and two workspaces' join data; prints one JSON line, serves until stdin closes. Not a test."""

import json
import sys
import tempfile
from pathlib import Path

hub_src = Path(sys.argv[1])
sys.path[:0] = [str(hub_src / "src"), str(hub_src / "tests")]

from orch_hub.contract import vectorgen as G  # noqa: E402
from orch_hub.contract.canon import b64u  # noqa: E402
from orch_hub.keys import HubKeys  # noqa: E402
from orch_hub.link import LinkServer, Registry  # noqa: E402
from orch_hub.link.tls import TlsIdentity  # noqa: E402
from orch_hub.router import Limits, RouteQueue, Router  # noqa: E402

from conftest import Invites  # noqa: E402


def proof(o: dict, ws: str, pk: str = "pk_alice") -> dict:
    """#1198 C9: the owner proof (protocol 9.6) the real hub wants with a join, made now (its clock is real)."""
    import time

    now = int(time.time() * 1000)
    payload = {k: o[k] for k in ("hub_id", "hk_root_pub", "mode", "binding_seq")} | {"aliases": []}
    return G.owner_proof("hub.join", payload, pk=pk, workspace_id=ws, issued_ms=now - 1_000, expires_ms=now + 300_000)


with tempfile.TemporaryDirectory() as d:
    keys = HubKeys.init(Path(d) / "hub")
    ident = TlsIdentity.generate()
    reg = Registry()
    queue = RouteQueue(str(Path(d) / "q.db"), Limits())
    router = Router(reg, queue, None, keys._now)
    srv = LinkServer.from_keys(
        keys,
        identity=ident,
        registry=reg,
        invites=Invites(invA=None, invB=None),
        router=router,
        dev_mode=True,
    ).start()
    card = keys.card()

    def ws(w: str, wsk: str, invite: str) -> dict:
        o = {**G.binding(ws=w, wsk=wsk)["o"], "hub_id": keys.hub_id, "hk_root_pub": b64u(keys.hk_root_pub)}
        return {
            "workspace_id": w,
            "wsk_scalar": G.K.priv[wsk].private_numbers().private_value,
            "join": {
                "invite": invite,
                "card": G.v2_card(ws=w, wsk=wsk),
                "pk_pub": b64u(G.K.pub["pk_alice"]),
                "binding": G.signed(wsk, "sig_hub_binding", o),
                "owner_proof": proof(o, w),
            },
        }

    print(
        json.dumps(
            {
                "port": srv.address[1],
                "spki": card["o"]["tls_spki_sha256"],
                "hub_id": keys.hub_id,
                "hk_op_pub": keys.op_cert()["o"]["hk_op_pub"],
                "a": ws(G.WS_A, "wsk_a", "invA"),
                "b": ws(G.WS_B, "wsk_b", "invB"),
            }
        ),
        flush=True,
    )
    sys.stdin.read()
    srv.close()
    reg.close()
    queue.close()
