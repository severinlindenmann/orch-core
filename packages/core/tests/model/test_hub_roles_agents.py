"""#1133 / #1134: in full mode the hub role caps an aliased person's agents and counts hub users as people."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orch.model import authz, claims, policies
from orch.model.codes import Code
from orch.model.types import ClaimCore, Core, Grant
from tests.model.test_hub_replay import P, ws_with
from tests.model.test_hubledger import U1, U2

AT = "2026-10-09T09:10:11Z"
ALIAS = {"4" * 32: U1}


def agent_event(ws, role_scope="workable"):
    ws.grants["g1"] = Grant("g1", P, role_scope, "agent", "2026-10-09T08:00:00Z", "2026-10-10T08:00:00Z")
    return {"type": "log.added", "at": AT, "actor": {"kind": "agent", "grant": "g1", "for": P, "session": "s_1"}}


def refusal(ws, member="member", scope="workable"):
    return authz.authorize_agent(Core(ws=ws), None, agent_event(ws, scope))


def claim_ticket(ws):
    c = ClaimCore("s_1", P, "g1", 0, authz.ts(AT) - 10)
    return SimpleNamespace(claim=c)


def full(roles, member="member", mode="full"):
    ws = ws_with(mode, ALIAS, {P: member}, roles)
    ws.settings = {"claim_ttl_min": 60}
    return ws


def test_agent_of_an_aliased_member_with_a_hub_role_works():
    assert refusal(full({U1: "edit"})) is None


@pytest.mark.parametrize("roles", [{}, {U1: "read"}], ids=["removed", "demoted"])
def test_hub_removal_or_demotion_stops_the_agent_and_lapses_the_claim(roles):
    ws = full(roles)
    r = refusal(ws)
    assert r is not None and r.code == Code.GRANT_INVALID
    assert claims.lapse_reason(ws, claim_ticket(ws), authz.ts(AT)) == "member_removed"


def test_console_link_owner_alias_keeps_the_owners_agents_working():
    """Run-12 6.2: the join records the link's hub user as the owner's alias, so the hub admin role carries over."""
    ws = ws_with("full", ALIAS, {P: "owner"}, {U1: "admin"})
    ws.settings = {"claim_ttl_min": 60}
    assert authz.authorize_agent(Core(ws=ws), None, agent_event(ws)) is None
    assert claims.lapse_reason(ws, claim_ticket(ws), authz.ts(AT)) is None


def test_unaliased_members_stay_refused_in_full_mode():
    ws = ws_with("full", {}, {P: "owner"}, {U1: "admin"})
    r = authz.authorize_agent(Core(ws=ws), None, agent_event(ws))
    assert r is not None and r.code == Code.GRANT_INVALID


def test_hub_removed_owner_is_refused():
    ws = ws_with("full", ALIAS, {P: "owner"}, {})
    r = authz.authorize_agent(Core(ws=ws), None, agent_event(ws))
    assert r is not None and r.code == Code.GRANT_INVALID


def test_live_claim_with_a_hub_role():
    ws = full({U1: "edit"})
    agent_event(ws)
    assert claims.lapse_reason(ws, claim_ticket(ws), authz.ts(AT)) is None


def test_hub_role_caps_an_owner_grant_to_workable():
    ws = full({U1: "edit"}, member="owner")
    assert refusal(ws, scope="all").code == Code.GRANT_SCOPE
    assert refusal(full({U1: "admin"}, member="owner"), scope="all") is None


@pytest.mark.parametrize("mode", ["selective"])
def test_selective_ignores_the_hub_role(mode):
    ws = full({}, mode=mode)
    assert refusal(ws) is None
    assert claims.lapse_reason(ws, claim_ticket(ws), authz.ts(AT)) is None


def test_standalone_unchanged():
    ws = full({U1: "read"})
    ws.hub = {}
    assert refusal(ws) is None


def test_solo_counts_hub_users_in_full_mode():
    assert policies.solo(full({U1: "admin"}))
    assert not policies.solo(full({U1: "admin", U2: "edit"}))
    assert not policies.solo(full({U1: "admin", U2: "read"}))
    assert policies.solo(full({U1: "admin", U2: None}))  # no role: not a person here
    assert policies.solo(full({U1: "admin", U2: "bogus"}))
    assert policies.solo(full({U1: "admin", U2: "edit"}, mode="selective"))
    ws = full({U1: "admin"})
    ws.hub = {}
    assert policies.solo(ws)
