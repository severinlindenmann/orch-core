# ruff: noqa: F811
"""list_shared: who may read a selective workspace's shared items (spec §8a), and that what they read is verified."""

from __future__ import annotations

from shares_helpers import ADMIN, IT1, IT2, OTHER, OWNER, body, shub  # noqa: F401

from orch_hub.access import Access
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import unb64u


def test_only_the_aliased_owner_sees_items(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body(IT2, 1, title="b"))
    c.request("share_put", body(IT1, 1, title="a"))
    got = h.svc.list_shared(OWNER, G.WS_A)
    assert [i["item_id"] for i in got] == sorted([IT1, IT2])
    one = next(i for i in got if i["item_id"] == IT1)
    assert one["sealed"] == unb64u(body(IT1, 1, title="a")["sealed"])
    assert one["item"]["content"]["title"] == "a" and one["item"]["item_version"] == 1  # opened and verified
    assert one["hash"].startswith("sha256:")
    assert h.svc.list_shared(OTHER, G.WS_A) == []
    assert h.svc.list_shared("", G.WS_A) == []
    assert h.svc.list_shared(None, G.WS_A) == []  # type: ignore[arg-type]


def test_revoked_items_are_not_listed(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body(IT1, 1))
    c.request("share_revoke", {"item_id": IT1})
    assert h.svc.list_shared(OWNER, G.WS_A) == []


def test_an_item_that_no_longer_verifies_is_left_out(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body(IT1, 1))
    h.svc._hub = G.fake16("some_other_hub")  # e.g. a state dir moved to another hub: fail closed
    assert h.svc.list_shared(OWNER, G.WS_A) == []


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
    h.join().request("share_put", body())
    # the service has no Access at all: roles cannot widen the read
    assert h.svc.list_shared(ADMIN, G.WS_A) == [] and h.svc.list_shared(OTHER, G.WS_A) == []
    assert len(h.svc.list_shared(OWNER, G.WS_A)) == 1
    acc.close()


def test_nothing_after_unbind_or_for_unknown_workspace(shub):
    h = shub()
    c = h.join()
    c.request("share_put", body())
    assert h.svc.list_shared(OWNER, G.WS_B) == []
    c.request("binding_put", {"object": G.unbound(seq=1)})
    assert h.svc.list_shared(OWNER, G.WS_A) == []


def test_no_alias_means_nobody(shub):
    h = shub()
    h.invites.owner_user = lambda inv: None
    h.join().request("share_put", body())
    assert h.registry.owner_alias(G.WS_A) is None
    assert h.svc.list_shared(OWNER, G.WS_A) == []


def test_granted_user_without_member_alias_reads_nothing(shub):
    """L2P4-1: a user the owner granted in a selective workspace but did not alias to a member is a viewer. The hub only
    knows the owner alias (grants live at the host), so list_shared keeps the owner-alias-only rule: a granted user
    reads nothing here, and the contract (§10.2b, §10.5) adds no hub-side read rule for grants."""
    h = shub()
    h.join().request("share_put", body())
    assert h.svc.list_shared(OTHER, G.WS_A) == []
    assert len(h.svc.list_shared(OWNER, G.WS_A)) == 1
