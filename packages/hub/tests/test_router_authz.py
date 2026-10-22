# ruff: noqa: F811, RUF059  (the `rhub` fixture is imported then named as an argument; setup() returns a full tuple)
"""V3-D19 over real links: a route A->B needs A's aliased owner to hold edit or admin in B (full mode)."""

from __future__ import annotations

import pytest
from router_helpers import RouterHub, envelope, rhub  # noqa: F401

from orch_hub.access import Access
from orch_hub.contract import vectorgen as G
from orch_hub.link import LinkError
from orch_hub.router import AccessAuthz

U = G.U_ALICE


class AliasInvites:
    """The conftest invite gate plus the admin's owner alias per invite."""

    def __init__(self, inner, aliases):
        self._i, self._a = inner, aliases

    def check(self, invite, now_ms):
        return self._i.check(invite, now_ms)

    def consume(self, invite, ws):
        self._i.consume(invite, ws)

    def owner_user(self, invite):
        return self._a.get(invite)


def setup(rhub, *, alias=U, role="edit", mode_b="full"):
    acc = Access()
    h = rhub()
    h.router._authz = AccessAuthz(h.registry, acc)
    h.server._invites = AliasInvites(h.invites, {"inv1": alias} if alias else {})
    acc.add_user(U, "alice")
    acc.add_group("eng")
    acc.add_member("eng", U)
    if role:
        acc.grant(G.WS_B, "eng", role)
    a = h.open(first=True)
    a.request("join", h.join_body())
    b = h.open(G.WS_B, "wsk_b", first=True)
    b.request("join", h.join_body("invB", ws=G.WS_B, wsk="wsk_b", mode=mode_b))
    return h, acc, a, b


def refused(c, env, code="not_allowed"):
    with pytest.raises(LinkError) as e:
        c.route_out(env)
    assert e.value.code == code


@pytest.mark.parametrize("role", ["edit", "admin"])
def test_allowed_with_edit_or_admin(rhub, role):
    h, acc, a, b = setup(rhub, role=role)
    env = envelope()
    assert a.route_out(env) in ("forwarded", "queued")
    assert b.next_route_in() == env


@pytest.mark.parametrize("role", ["read", None])
def test_refused_with_read_or_no_role(rhub, role):
    h, acc, a, b = setup(rhub, role=role)
    refused(a, envelope())
    assert h.queue.depth(G.WS_B) == 0


def test_refused_without_an_owner_alias(rhub):
    h, acc, a, b = setup(rhub, alias=None)
    assert h.registry.owner_alias(G.WS_A) is None
    refused(a, envelope())


def test_alias_is_stored_from_the_admins_invite_at_join(rhub):
    h, acc, a, b = setup(rhub)
    assert h.registry.owner_alias(G.WS_A) == U
    assert h.registry.owner_alias(G.WS_B) is None  # invB carries none


def test_revoked_role_stops_the_next_route(rhub):
    h, acc, a, b = setup(rhub)
    a.route_out(envelope())
    acc.revoke(G.WS_B, "eng")
    refused(a, envelope())
    acc.grant(G.WS_B, "eng", "edit")
    a.route_out(envelope())  # and a new grant opens it again


def test_removed_from_group_stops_the_next_route(rhub):
    h, acc, a, b = setup(rhub)
    acc.remove_member("eng", U)
    refused(a, envelope())


def test_selective_recipient_gets_nothing_from_hub_roles(rhub):
    h, acc, a, b = setup(rhub, mode_b="selective")
    refused(a, envelope())


def test_direction_matters(rhub):
    h, acc, a, b = setup(rhub)
    refused(b, envelope(G.WS_B, G.WS_A, key="wsk_b"))  # B has no alias


def test_access_errors_fail_closed(rhub):
    class Broken:
        def role_of(self, u, w):
            raise RuntimeError("store down")

    h, acc, a, b = setup(rhub)
    h.router._authz = AccessAuthz(h.registry, Broken())
    refused(a, envelope())


def test_fixed_invites_parse_owner_alias():
    from orch_hub.serve import FixedInvites

    inv = FixedInvites([f"code1={U}", "plain", "weird=notahexid"])
    assert inv.owner_user("code1") == U and inv.owner_user("plain") is None
    assert inv.check("code1", 0) is None and inv.check("weird=notahexid", 0) is None
