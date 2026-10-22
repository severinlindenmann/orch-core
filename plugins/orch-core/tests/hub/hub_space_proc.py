"""Run by the cross-repo space test under orch-hub's own environment (``ORCH_HUB_SRC``): the real link server with its
router, access store and SpaceService, two workspaces' join data (modes from argv), and a tiny admin command loop on
stdin (one JSON object per line -> one JSON line back). Not a test."""

import json
import sys
import tempfile
from pathlib import Path

hub_src = Path(sys.argv[1])
modes = {"a": sys.argv[2], "b": sys.argv[3], "c": "full"}
sys.path[:0] = [str(hub_src / "src"), str(hub_src / "tests")]

from orch_hub.access import Access  # noqa: E402
from orch_hub.contract import vectorgen as G  # noqa: E402
from orch_hub.contract.canon import b64u  # noqa: E402
from orch_hub.keys import HubKeys  # noqa: E402
from orch_hub.link import LinkServer, Registry  # noqa: E402
from orch_hub.link.tls import TlsIdentity  # noqa: E402
from orch_hub.router import Limits, RouteQueue, Router  # noqa: E402
from orch_hub.spaces import SpaceService, SpaceStore  # noqa: E402
from orch_hub.spaces.service import SpaceError  # noqa: E402

from conftest import Invites  # noqa: E402

ADMIN = G.fake16("hub_admin_user")

with tempfile.TemporaryDirectory() as d:
    keys = HubKeys.init(Path(d) / "hub")
    ident = TlsIdentity.generate()
    reg = Registry()
    access = Access()
    access.add_user(ADMIN, "admin")
    access.add_member("hub-admin", ADMIN)
    queue = RouteQueue(str(Path(d) / "q.db"), Limits())
    router = Router(reg, queue, None, keys._now)
    spaces = SpaceService(
        SpaceStore(str(Path(d) / "spaces.db")), reg, access, keys.op_signer, keys.hub_id,
        lambda: keys.op_cert()["o"]["cert_id"], now_ms=keys._now,
    )  # fmt: skip
    srv = LinkServer.from_keys(
        keys,
        identity=ident,
        registry=reg,
        invites=Invites(invA=None, invB=None, invC=None),
        router=router,
        access=access,
        spaces=spaces,
    ).start()
    card = keys.card()

    def ws(w: str, wsk: str, wxk: str, invite: str, mode: str) -> dict:
        o = {
            **G.binding(ws=w, wsk=wsk, mode=mode)["o"],
            "hub_id": keys.hub_id,
            "hk_root_pub": b64u(keys.hk_root_pub),
        }
        return {
            "workspace_id": w,
            "wsk_scalar": G.K.priv[wsk].private_numbers().private_value,
            "wxk_scalar": G.K.priv[wxk].private_numbers().private_value,
            "join": {
                "invite": invite,
                "card": G.v2_card(ws=w, wsk=wsk, wxk_pub=G.K.b(wxk)),
                "pk_pub": b64u(G.K.pub["pk_alice"]),
                "binding": G.signed(wsk, "sig_hub_binding", o),
            },
        }

    print(
        json.dumps(
            {
                "port": srv.address[1],
                "spki": card["o"]["tls_spki_sha256"],
                "hub_id": keys.hub_id,
                "hk_op_pub": keys.op_cert()["o"]["hk_op_pub"],
                "hub_card": card,
                "a": ws(G.WS_A, "wsk_a", "wxk_a", "invA", modes["a"]),
                "b": ws(G.WS_B, "wsk_b", "wxk_b", "invB", modes["b"]),
                "c": ws(G.WS_C, "wsk_a", "wxk_a", "invC", modes["c"]),
            }
        ),
        flush=True,
    )
    for line in sys.stdin:
        cmd = json.loads(line)
        try:
            if cmd["cmd"] == "create":
                out = spaces.create("launch", cmd["members"], ADMIN)
            elif cmd["cmd"] == "add":
                out = spaces.add(cmd["space_id"], cmd["ws"], ADMIN)
            elif cmd["cmd"] == "remove":
                out = spaces.remove(cmd["space_id"], cmd["ws"], ADMIN)
            else:
                out = {"error": "unknown_cmd"}
        except SpaceError as e:
            out = {"error": e.code, "need": e.need}
        print(json.dumps(out), flush=True)
    srv.close()
    reg.close()
    queue.close()
