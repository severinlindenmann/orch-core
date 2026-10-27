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

    inv = FixedInvites([f"code1={U}", "plain"])
    assert inv.owner_user("code1") == U and inv.owner_user("plain") is None
    assert inv.check("code1", 0) is None


@pytest.mark.parametrize("bad", ["weird=notahexid", "x=" + U[:-1], "x=" + U.upper(), "x=", f"={U}"])
def test_fixed_invites_reject_a_bad_alias(bad):
    from orch_hub.serve import FixedInvites

    with pytest.raises(ValueError, match=r"invite #2"):
        FixedInvites(["ok", bad])


def test_cli_exits_with_an_error_for_a_bad_alias(tmp_path, capsys):
    from orch_hub.cli import main

    assert (
        main(
            [
                "serve",
                "--state",
                str(tmp_path),
                "--listen",
                "127.0.0.1:0",
                "--publish-url",
                "http://127.0.0.1:1",
                "--invite",
                "a=zz",
            ]
        )
        == 1
    )
    assert "invite #1" in capsys.readouterr().err


def rejoin(h, a, b, invite_alias):
    """B leaves (hub.unbound), then A leaves and joins again with a new invite."""
    a.request("binding_put", {"object": G.unbound(seq=1)})
    h.invites.codes["inv2"] = None
    h.server._invites = AliasInvites(h.invites, {"inv2": invite_alias} if invite_alias else {})
    a2 = h.open(first=True)
    a2.request("join", h.join_body("inv2", seq=2))
    return a2


def test_rejoin_without_alias_deletes_the_old_one(rhub):
    h, acc, a, b = setup(rhub)
    assert h.registry.owner_alias(G.WS_A) == U
    a2 = rejoin(h, a, b, None)
    assert h.registry.owner_alias(G.WS_A) is None
    refused(a2, envelope())


def test_rejoin_with_another_alias_replaces_it(rhub):
    h, acc, a, b = setup(rhub)
    other = G.U_BOB
    a2 = rejoin(h, a, b, other)
    assert h.registry.owner_alias(G.WS_A) == other
    refused(a2, envelope())  # bob has no role in B


def test_alias_is_committed_with_the_join_or_not_at_all(rhub, monkeypatch):
    h, acc, a, b = setup(rhub)
    reg = h.registry
    real = reg._db.execute

    # a failure inside commit_join (after the pin row, before the alias row) must roll the whole join back
    def boom(sql, *args):
        if "owner_alias" in sql and sql.startswith("INSERT"):
            raise RuntimeError("disk")
        return real(sql, *args)

    class Wrap:
        def __init__(self, db):
            self._db = db

        def execute(self, sql, *args):
            return boom(sql, *args)

        def __enter__(self):
            return self._db.__enter__()

        def __exit__(self, *e):
            return self._db.__exit__(*e)

        def __getattr__(self, n):
            return getattr(self._db, n)

    before = reg.binding_state(G.WS_A)
    reg._db = Wrap(reg._db)
    with pytest.raises(RuntimeError):
        reg.commit_join(
            G.WS_A,
            reg.pin(G.WS_A)["owner_person_id"],
            reg.pin(G.WS_A)["wsk_pub"],
            reg.pin(G.WS_A)["pk_pub"],
            reg.card(G.WS_A),
            {"binding_seq": 9, "status": "bound", "binding": before["binding"]},
            owner_alias=G.U_BOB,
        )
    reg._db = reg._db._db
    assert reg.binding_state(G.WS_A) == before and reg.owner_alias(G.WS_A) == U


def test_unbound_sender_is_refused_by_authz_itself(rhub):
    h, acc, a, b = setup(rhub)
    a.request("binding_put", {"object": G.unbound(seq=1)})
    assert AccessAuthz(h.registry, acc).may_route(G.WS_A, G.WS_B) is False


def test_the_signed_alias_must_be_the_invites_bound_owner(rhub):
    """Run 12: a forged owner in the connect link ends in a refused join; the matching one is accepted."""
    h = rhub()
    h.server._invites = AliasInvites(h.invites, {"inv1": U})
    forged = h.open(first=True)
    with pytest.raises(LinkError) as e:
        forged.request("join", {**h.join_body(), "alias_users": [G.U_BOB]})
    assert e.value.code == "owner_alias_mismatch"
    assert h.registry.owner_alias(G.WS_A) is None
    ok = h.open(first=True)
    ok.request("join", {**h.join_body(), "alias_users": [U]})
    assert h.registry.owner_alias(G.WS_A) == U


def test_an_alias_without_a_bound_owner_is_refused(rhub):
    h = rhub()
    h.server._invites = AliasInvites(h.invites, {})
    c = h.open(first=True)
    with pytest.raises(LinkError) as e:
        c.request("join", {**h.join_body(), "alias_users": [U]})
    assert e.value.code == "owner_alias_mismatch"
