"""Helpers of the shared-item tests: a hub with a real ShareService (and a private shares.db) over real links."""

from __future__ import annotations

import pytest
from conftest import NOW, Hub

from orch_hub.access import Access
from orch_hub.audit import AuditLog, AuditStore
from orch_hub.contract import keystore as KS
from orch_hub.contract import vectorgen as G
from orch_hub.contract import vectorgen_p4 as V4
from orch_hub.link import LinkError, Registry
from orch_hub.shares import ShareService, ShareStore

OWNER = G.fake16("owner_user")  # the hub user the admin aliased as WS_A's owner
OTHER = G.fake16("other_user")
ADMIN = G.fake16("hub_admin_user")
IT1, IT2, IT3 = G.fake16("it1"), G.fake16("it2"), G.fake16("it3")


class ShHub(Hub):
    def __init__(self, store_path: str = ":memory:", registry=None, access=None, **kw):
        self.share_store = ShareStore(store_path)
        reg = registry or Registry()
        self.events: list[dict] = []
        self.audit = AuditLog(
            AuditStore(),
            KS.OpSigner(G.K.priv["hk_op"]),
            G.HUB_ID,
            lambda: G.OP_CERT_ID,
            op_cert_by_id=lambda i: G.op_cert() if i == G.OP_CERT_ID else None,
            now_ms=lambda: NOW,
            dev_mode=True,
        )

        def on_change(ev):
            self.events.append(ev)
            self.audit.share_change(ev)

        self.svc = ShareService(
            self.share_store,
            reg,
            hub_id=G.HUB_ID,
            hxk=G.K.priv["hxk"],
            dev_mode=True,
            now_ms=lambda: NOW,
            on_change=on_change,
            access=access,
        )
        super().__init__(registry=reg, shares=self.svc, **({"access": access} if access is not None else {}), **kw)
        self.invites.owner_user = lambda inv: OWNER  # V3-D19: the admin's invite names the owner
        self.joined: dict[str, object] = {}

    def join(self, mode: str = "selective", ws: str = G.WS_A):
        if ws in self.joined:
            return self.joined[ws]
        c = self.open(ws, first=True)
        c.request("join", self.join_body(mode=mode))
        self.joined[ws] = c
        return c

    def close(self):
        super().close()
        self.share_store.close()


@pytest.fixture
def shub():
    made = []

    def make(**kw):
        h = ShHub(**kw)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def body(item_id: str = IT1, version: int = 1, title: str = "CSV or API for tariffs?", **kw) -> dict:
    """A real ``share_put`` body (contract §10.2b): a WSK-signed ``needs_you`` item sealed to the hub's HXK."""
    c = {**V4.NEEDS_YOU, "title": title}
    return V4.share_body(V4.share_item(version, item_id=item_id, content=c, **kw))


def audit_records(h) -> list[tuple[dict, dict]]:
    """``(record o, event)`` of every ``share`` record on the hub's audit chain."""
    return [(s["o"], ev) for _q, s, ev in h.audit.store.after(0, 1000) if s["o"]["type"] == "share"]


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


__all__ = [
    "ADMIN",
    "IT1",
    "IT2",
    "IT3",
    "OTHER",
    "OWNER",
    "Access",
    "ShHub",
    "audit_records",
    "body",
    "refused",
    "shub",
]
