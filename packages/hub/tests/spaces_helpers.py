"""Helpers of the shared-space tests: a hub with a real SpaceService and a fake member host that does what protocol.md
§10.4 says a host does (verify the record, open its wrap, seal and open items), with the contract's own checks."""

from __future__ import annotations

import pytest
from conftest import NOW, Hub

from orch_hub.access import Access
from orch_hub.audit import AuditLog, AuditStore
from orch_hub.contract import hub as H
from orch_hub.contract import keystore as KS
from orch_hub.contract import spaces as SP
from orch_hub.contract import suite as S
from orch_hub.contract import vectorgen as G
from orch_hub.link import Registry
from orch_hub.spaces import SpaceService, SpaceStore

ADMIN = G.fake16("hub_admin_user")  # a hub-admin
ALICE = G.U_ALICE  # admin in the full-mode workspaces she is granted
BOB = G.U_BOB
WS_C = G.WS_C
KEY = {G.WS_A: "wsk_a", G.WS_B: "wsk_b", WS_C: "wsk_a"}
WXK = {G.WS_A: "wxk_a", G.WS_B: "wxk_b", WS_C: "wxk_a"}


class SHub(Hub):
    def __init__(self, store_path: str = ":memory:", registry=None, access=None):
        self.access = access or Access()
        self.store = SpaceStore(store_path)
        reg = registry or Registry()
        self.audit = AuditLog(
            AuditStore(),
            KS.OpSigner(G.K.priv["hk_op"]),
            G.HUB_ID,
            lambda: G.OP_CERT_ID,
            op_cert_by_id=lambda i: G.op_cert() if i == G.OP_CERT_ID else None,
            now_ms=lambda: NOW,
            dev_mode=True,
        )
        self.svc = SpaceService(
            self.store,
            reg,
            self.access,
            KS.OpSigner(G.K.priv["hk_op"]),
            G.HUB_ID,
            lambda: G.OP_CERT_ID,
            now_ms=lambda: NOW,
            dev_mode=True,
            on_change=self.audit.space_change,
        )
        super().__init__(registry=reg, spaces=self.svc, audit=self.audit)
        self.access.add_user(ADMIN, "admin")
        self.access.add_member("hub-admin", ADMIN)
        self.access.observer = self.audit.access_change  # after the test fixture's own setup
        self.joined: dict[str, object] = {}

    def join(self, ws: str, mode: str = "full"):
        if ws in self.joined:
            return self.joined[ws]
        inv = {G.WS_A: "inv1", G.WS_B: "invB", WS_C: "inv3"}[ws]
        c = self.open(ws, KEY[ws], first=True)
        body = {
            "invite": inv,
            "card": G.v2_card(ws=ws, wsk=KEY[ws], wxk_pub=G.K.b(WXK[ws])),
            "pk_pub": G.b64u(G.K.pub["pk_alice"]),
            "binding": G.binding(ws=ws, wsk=KEY[ws], mode=mode),
        }
        c.request("join", body)
        self.joined[ws] = c
        return c

    def close(self):
        super().close()
        self.store.close()
        self.audit.store.close()
        self.access.close()


@pytest.fixture
def shub():
    made = []

    def make(**kw):
        h = SHub(**kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


HELD = H.verify_card(G.card(), now_ms=NOW, dev_mode=True)


class Host:
    """A member host over its link: keeps the verified record and the SK_e of every epoch it learned."""

    def __init__(self, link, ws: str):
        self.link, self.ws = link, ws
        self.priv = G.K.priv[WXK[ws]]
        self.space: dict | None = None
        self.sk: dict[int, bytes] = {}

    def sync(self, space_id: str, since: int = 0) -> dict:
        r = self.link.request("space_get", {"space_id": space_id, "since": since})
        recs = {x["o"]["epoch"]: x for x in [r["space"], *r["records"]]}
        for w in r["wraps"]:
            rec = SP.verify_space(recs[w["o"]["epoch"]], HELD, dev_mode=True)
            _, sk = SP.verify_space_wrap(
                w, HELD, rec, workspace_id=self.ws, wxk_priv=self.priv, wxk_version=1, dev_mode=True
            )
            self.sk[rec["epoch"]] = sk
        self.space = SP.verify_space(r["space"], HELD, dev_mode=True)
        return r

    def put_body(self, space_id: str, item_id: str, body: dict | None = None, epoch: int | None = None) -> dict:
        sp = self.space
        epoch = sp["epoch"] if epoch is None else epoch
        o = {
            "v": 2,
            "suite": 2,
            "kind": "space.item",
            "hv": 0,
            "hub_id": G.HUB_ID,
            "space_id": space_id,
            "epoch": epoch,
            "members_hash": SP.members_hash(sp["members"]),
            "item_id": item_id,
            "workspace_id": self.ws,
            "created_ms": NOW - 1,
            "body": body or {"kind": "note", "text": "hello"},
            "dev": True,
        }
        signed = KS.WskSigner(G.K.priv[KEY[self.ws]]).sign_object(o)
        sealed = SP.seal_item(self.sk[epoch], space_id, epoch, item_id, signed, salt=SP.new_salt())
        return {"space_id": space_id, "epoch": epoch, "item_id": item_id, "sealed": sealed}

    def read(self, space_id: str, r: dict, wsk_pubs: dict) -> list[dict]:
        out = []
        for e in r["entries"]:
            if e["revoked"]:
                continue
            members = next(h["members"] for h in self._history(r, e["epoch"], space_id))
            out.append(
                SP.verify_item(
                    e["sealed"],
                    sk=self.sk[e["epoch"]],
                    space_id=space_id,
                    epoch=e["epoch"],
                    item_id=e["item_id"],
                    members=members,
                    wsk_pubs=wsk_pubs,
                    hub_id=G.HUB_ID,
                    dev_mode=True,
                )
            )
        return out

    def _history(self, r, epoch, space_id):
        recs = {x["o"]["epoch"]: x["o"] for x in [r["space"], *r["records"]]}
        return [{"members": recs[epoch]["members"]}]


PUBS = {G.WS_A: G.K.pub["wsk_a"], G.WS_B: G.K.pub["wsk_b"], WS_C: G.K.pub["wsk_a"]}
_ = S
