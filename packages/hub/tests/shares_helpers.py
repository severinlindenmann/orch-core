"""Helpers of the shared-item tests: a hub with a real ShareService (and a private shares.db) over real links."""

from __future__ import annotations

import pytest
from conftest import NOW, Hub

from orch_hub.access import Access
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.link import LinkError, Registry
from orch_hub.shares import ShareService, ShareStore

OWNER = G.fake16("owner_user")  # the hub user the admin aliased as WS_A's owner
OTHER = G.fake16("other_user")
ADMIN = G.fake16("hub_admin_user")
IT1, IT2, IT3 = G.fake16("it1"), G.fake16("it2"), G.fake16("it3")


class ShHub(Hub):
    def __init__(self, store_path: str = ":memory:", registry=None, **kw):
        self.share_store = ShareStore(store_path)
        reg = registry or Registry()
        self.events: list[dict] = []
        self.svc = ShareService(self.share_store, reg, now_ms=lambda: NOW, on_change=self.events.append)
        super().__init__(registry=reg, shares=self.svc, **kw)
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


def sealed(tag: str, n: int = 40) -> str:
    """Opaque bytes standing in for a sealed item (the hub never parses them)."""
    return b64u((tag.encode() * n)[:n])


def refused(c, op, body, code):
    with pytest.raises(LinkError) as e:
        c.request(op, body)
    assert e.value.code == code


__all__ = ["ADMIN", "IT1", "IT2", "IT3", "OTHER", "OWNER", "Access", "ShHub", "refused", "sealed", "shub"]
