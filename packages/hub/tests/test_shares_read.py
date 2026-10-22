# ruff: noqa: F811
"""list_shared: who may read a selective workspace's shared items (spec §8a)."""

from __future__ import annotations

from shares_helpers import ADMIN, IT1, IT2, OTHER, OWNER, sealed, shub  # noqa: F401

from orch_hub.access import Access
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import unb64u


def test_only_the_aliased_owner_sees_items(shub):
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT2, "sealed": sealed("b")})
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    got = h.svc.list_shared(OWNER, G.WS_A)
    assert [i["item_id"] for i in got] == sorted([IT1, IT2])
    assert got[0]["sealed"] == unb64u(sealed("a")) if got[0]["item_id"] == IT1 else True
    assert h.svc.list_shared(OTHER, G.WS_A) == []
    assert h.svc.list_shared("", G.WS_A) == []
    assert h.svc.list_shared(None, G.WS_A) == []  # type: ignore[arg-type]


def test_roles_and_hub_admin_get_nothing_in_selective(shub):
    """Hub groups and the hub-admin group get no role in a selective workspace (§8a)."""
    acc = Access()
    acc.add_user(ADMIN, "admin")
    acc.add_member("hub-admin", ADMIN)
    acc.add_user(OTHER, "other")
    acc.add_group("team")
    acc.add_member("team", OTHER)
    acc.grant(G.WS_A, "team", "admin")
    assert acc.role_of(OTHER, G.WS_A) == "admin" and acc.is_hub_admin(ADMIN)
    h = shub()
    h.join().request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    # the service has no Access at all: roles cannot widen the read
    assert h.svc.list_shared(ADMIN, G.WS_A) == [] and h.svc.list_shared(OTHER, G.WS_A) == []
    assert len(h.svc.list_shared(OWNER, G.WS_A)) == 1
    acc.close()


def test_nothing_after_unbind_or_for_unknown_workspace(shub):
    h = shub()
    c = h.join()
    c.request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    assert h.svc.list_shared(OWNER, G.WS_B) == []
    c.request("binding_put", {"object": G.unbound(seq=1)})
    assert h.svc.list_shared(OWNER, G.WS_A) == []


def test_no_alias_means_nobody(shub):
    h = shub()
    h.invites.owner_user = lambda inv: None
    h.join().request("share_put", {"item_id": IT1, "sealed": sealed("a")})
    assert h.registry.owner_alias(G.WS_A) is None
    assert h.svc.list_shared(OWNER, G.WS_A) == []
