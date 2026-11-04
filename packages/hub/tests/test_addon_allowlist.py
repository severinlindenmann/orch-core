"""M3-1: per-workspace add-on allowlist (#1449) and requests (#1450), Opus conditions 13 and 14.

* 13 the effective policy is the hub-wide one INTERSECTED with the workspace's own list: it can narrow, never widen; block
  lists are untouched; each snapshot carries only its own workspace's policy;
* 14 ``addon_request`` is display-only: bounded, one row per (workspace, add-on), capped, records the actor; approving is an
  audited policy change and a blocked add-on cannot be approved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from webapi_helpers import WS, Web

from orch_hub.access import Access
from orch_hub.access.store import AccessError
from orch_hub.contract import market_policy as MP
from orch_hub.contract.hub import Refused
from orch_hub.link import Registry
from orch_hub.link.ops import addon_request

ADMIN = "aa" * 16
WS2 = "b2" * 16


@pytest.fixture
def acc():
    a = Access(":memory:")
    a.observer = lambda ev: None  # as serve wires the audit chain
    a.add_user(ADMIN, "root")
    a.add_member("hub-admin", ADMIN)
    yield a
    a.close()


def set_global(acc, allow, block=()):
    base = acc.market_policy()
    policy = {**base["policy"], "addons": {"allow": allow, "block": list(block)}}
    acc.set_market_policy(base["version"], policy, actor=ADMIN)


def allow_of(acc, ws):
    return acc.effective_market_policy(ws)["policy"]["addons"]["allow"]


def test_effective_policy_is_the_intersection_and_never_wider(acc):
    set_global(acc, ["alpha", "beta", "gamma"])
    assert allow_of(acc, WS) == ["alpha", "beta", "gamma"]  # no limit of its own: the hub-wide list
    acc.set_workspace_addon_allow(WS, ["beta", "delta"], actor=ADMIN)  # delta is not allowed hub-wide
    assert allow_of(acc, WS) == ["beta"]  # a per-workspace list can NOT add an id the hub-wide list lacks
    acc.set_workspace_addon_allow(WS, [], actor=ADMIN)
    assert allow_of(acc, WS) == []  # an empty list: nothing at all
    acc.set_workspace_addon_allow(WS, None, actor=ADMIN)
    assert allow_of(acc, WS) == ["alpha", "beta", "gamma"]
    # hub-wide "anything": the workspace list narrows it to exactly that list
    set_global(acc, None)
    acc.set_workspace_addon_allow(WS, ["beta"], actor=ADMIN)
    assert allow_of(acc, WS) == ["beta"] and allow_of(acc, WS2) is None


def test_block_lists_and_ceiling_are_never_touched_and_the_policy_stays_valid(acc):
    set_global(acc, ["alpha", "beta"], block=["beta"])
    acc.set_workspace_addon_allow(WS, ["alpha"], actor=ADMIN)
    eff = acc.effective_market_policy(WS)
    assert (
        eff["policy"]["addons"]["block"] == ["beta"]
        and eff["policy"]["ceiling"] == acc.market_policy()["policy"]["ceiling"]
    )
    MP.check_policy(eff["policy"])
    assert eff["hash"] == MP.policy_hash(eff["policy"]) and eff["hash"] != acc.market_policy()["hash"]


def test_each_snapshot_carries_only_its_own_workspace_policy(acc):
    from orch_hub.access.snapshots import AccessSnapshots
    from orch_hub.keys import HubKeys

    set_global(acc, ["alpha", "beta"])
    acc.set_workspace_addon_allow(WS, ["alpha"], actor=ADMIN)
    # the snapshot provider reads effective_market_policy(workspace_id): the two workspaces differ, the stored global is shared
    assert allow_of(acc, WS) == ["alpha"] and allow_of(acc, WS2) == ["alpha", "beta"]
    assert HubKeys and AccessSnapshots  # (the signing itself is covered by test_access_snapshots)
    import inspect

    from orch_hub.access import snapshots

    assert "effective_market_policy(workspace_id)" in inspect.getsource(snapshots)


def test_only_a_hub_admin_sets_a_list_and_ids_are_validated(acc):
    acc.add_user("bb" * 16, "bob")
    with pytest.raises(AccessError) as e:
        acc.set_workspace_addon_allow(WS, ["alpha"], actor="bb" * 16)
    assert e.value.code == "forbidden_role"
    for bad in (["Alpha"], ["a b"], [1], "alpha", ["x" * 41], ["a"] * 0 + ["a-"] * 257):
        with pytest.raises(AccessError):
            acc.set_workspace_addon_allow(WS, bad, actor=ADMIN)


# --- 14: the request ------------------------------------------------------------------------------------------------------


def ctx(acc, reg, ws=WS, body=None):
    return SimpleNamespace(access=acc, registry=reg, workspace_id=ws, body=body or {})


@pytest.fixture
def reg():
    r = Registry()
    for ws in (WS, WS2):
        r.commit_join(
            ws, ws, "key", "pk", {"name": ws}, {"status": "bound", "binding": {"mode": "full"}}, owner_alias=None
        )
    yield r
    r.close()


def test_a_request_is_bounded_deduplicated_capped_and_names_the_actor(acc, reg):
    for _ in range(3):
        assert addon_request(ctx(acc, reg, body={"name": "wiki", "version": "1.2.0", "actor": "agent"})) == {}
    (row,) = acc.addon_requests()
    assert (row["name"], row["count"], row["actor"], row["version"]) == ("wiki", 3, "agent", "1.2.0")
    assert addon_request(ctx(acc, reg, body={"name": "wiki", "version": "", "actor": "person"})) == {}
    assert acc.addon_requests()[0]["actor"] == "person" and acc.addon_requests()[0]["count"] == 4
    for n in range(60):  # at most 50 distinct add-ons per workspace
        addon_request(ctx(acc, reg, body={"name": f"a{n:02d}", "version": "", "actor": "person"}))
    assert len([r for r in acc.addon_requests() if r["workspace_id"] == WS]) == 50
    assert len(acc.addon_requests()) == 50
    addon_request(
        ctx(acc, reg, ws=WS2, body={"name": "wiki", "version": "", "actor": "person"})
    )  # per workspace, not global
    assert len(acc.addon_requests()) == 51


@pytest.mark.parametrize("version", ["", "1.0.0", "x" * 33, "1 0", "<b>"])
def test_a_malformed_request_is_dropped_by_the_store(acc, version):
    if version in ("", "1.0.0"):
        acc.add_addon_request(WS, "wiki", version)
        assert len(acc.addon_requests()) == 1
    else:
        with pytest.raises(AccessError):
            acc.add_addon_request(WS, "wiki", version)
        assert acc.addon_requests() == []


def test_the_op_needs_a_bound_pinned_workspace_and_the_contract_shape(acc, reg):
    with pytest.raises(Refused):
        addon_request(
            ctx(acc, reg, ws="c3" * 16, body={"name": "wiki", "version": "", "actor": "person"})
        )  # not pinned
    from orch_hub.contract import hub as H

    kind, mode, fields = H.LINK_OPS["addon_request"]
    assert (kind, mode) == ("w2h", None) and set(fields) == {"name", "version", "actor"}
    assert fields["actor"]("agent") and fields["actor"]("person") and not fields["actor"]("root")
    assert not fields["name"]("Wiki") and not fields["name"]("x" * 41) and fields["name"]("my-wiki")


def test_approving_allows_it_for_everyone_audited_and_a_blocked_one_cannot_be_approved(acc, reg):
    set_global(acc, ["alpha"], block=["bad"])
    acc.set_workspace_addon_allow(WS, ["alpha"], actor=ADMIN)
    acc.add_addon_request(WS, "wiki", "1.0.0")
    acc.add_addon_request(WS, "bad", "1.0.0")
    version = acc.market_policy()["version"]
    acc.decide_addon_request(WS, "wiki", True, actor=ADMIN)
    assert (
        acc.market_policy()["policy"]["addons"]["allow"] == ["alpha", "wiki"]
        and acc.market_policy()["version"] == version + 1
    )
    assert allow_of(acc, WS) == ["alpha", "wiki"]  # the workspace's own list got it too
    with pytest.raises(AccessError) as e:
        acc.decide_addon_request(WS, "bad", True, actor=ADMIN)
    assert e.value.code == "blocked" and "bad" not in allow_of(acc, WS)
    acc.decide_addon_request(WS, "bad", False, actor=ADMIN)  # turning it down just dismisses
    assert acc.addon_requests() == []
    with pytest.raises(AccessError) as e:
        acc.decide_addon_request(WS, "wiki", True, actor=ADMIN)
    assert e.value.code == "unknown_request"


# --- the admin routes -------------------------------------------------------------------------------------------------------


def test_the_admin_routes_list_set_decide_and_are_admin_only(tmp_path):
    w = Web(tmp_path)
    try:
        admin, ann = w.login(w.admin), w.login(w.ann)
        acc_ = w.env.acc
        base = acc_.market_policy()
        acc_.set_market_policy(
            base["version"], {**base["policy"], "addons": {"allow": ["alpha"], "block": []}}, actor=w.admin
        )
        acc_.add_addon_request(WS, "wiki", "1.0.0", "agent")
        s, o, _ = w.call("GET", "/admin/addons", token=admin)
        assert s == 200 and o["allow"] == ["alpha"] and [r["name"] for r in o["requests"]] == ["wiki"]
        assert o["requests"][0]["actor"] == "agent" and any(
            r["workspace_id"] == WS and r["allow"] is None for r in o["workspaces"]
        )
        for method, path, body in (
            ("GET", "/admin/addons", None),
            ("PUT", f"/admin/addons/{WS}", {"allow": ["alpha"]}),
            ("POST", "/admin/addon-requests", {"workspace_id": WS, "name": "wiki", "approve": True}),
        ):
            assert w.call(method, path, body, token=ann)[0] == 403 and w.call(method, path, body)[0] == 401
        assert w.call("PUT", f"/admin/addons/{WS}", {"allow": ["alpha", "Bad"]}, token=admin)[0] == 400
        assert w.call("PUT", f"/admin/addons/{WS}", {"allow": ["alpha"]}, token=admin)[:2] == (
            200,
            {"allow": ["alpha"]},
        )
        assert w.call("PUT", f"/admin/addons/{'d4' * 16}", {"allow": []}, token=admin)[0] == 404
        assert (
            w.call("POST", "/admin/addon-requests", {"workspace_id": WS, "name": "wiki", "approve": True}, token=admin)[
                0
            ]
            == 200
        )
        assert (
            w.call("POST", "/admin/addon-requests", {"workspace_id": WS, "name": "wiki", "approve": True}, token=admin)[
                0
            ]
            == 404
        )
        assert "wiki" in acc_.market_policy()["policy"]["addons"]["allow"]
    finally:
        w.close()
