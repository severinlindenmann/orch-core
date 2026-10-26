"""D14 (orch-core#927): the hub marketplace policy: the production validator against the shared vectors, the store
(default, compare-and-set, audit in the same step), the snapshot (required field, version bump) and the admin routes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from webapi_helpers import Web

from orch_hub.access import Access, AccessError, AccessSnapshots
from orch_hub.contract import hub as H
from orch_hub.contract import market_policy as MP
from orch_hub.contract.hub import Refused
from orch_hub.keys import HubKeys

DOC = json.loads((Path(__file__).resolve().parents[1] / "vectors" / "vectors_hub_policy.json").read_text())
POLICY_CASES = [c for c in DOC["cases"] if c["check"] == "policy"]
ADMIN, USER, WS = "0a" * 16, "0b" * 16, "a1" * 16


def company_policy() -> dict:
    ok = next(c for c in POLICY_CASES if c["name"] == "policy_company_one_key")["args"]["policy"]
    return ok


@pytest.mark.parametrize("case", POLICY_CASES, ids=[c["name"] for c in POLICY_CASES])
def test_validator_agrees_with_the_vectors(case):
    p = case["args"]["policy"]
    if "error" in case["expect"]:
        assert not MP.valid_policy(p)
        with pytest.raises(MP.PolicyMalformed):
            MP.check_policy(p)
    else:
        assert MP.check_policy(p) is p and MP.policy_hash(p) == case["expect"]["policy_hash"]


def test_the_vector_file_states_the_validators_capability_list():
    assert tuple(DOC["capabilities"]) == MP.CAPABILITIES


def test_default_is_official_only_and_matches_the_vector():
    d = MP.default_policy()
    assert [m["url"] for m in d["markets"]] == [MP.OFFICIAL_URL] and d["addons"] == {"allow": None, "block": []}
    assert d["ceiling"] is None
    dv = next(c for c in POLICY_CASES if c["name"] == "policy_default")
    assert dv["args"]["policy"] == d and MP.policy_hash(d) == dv["expect"]["policy_hash"]


@pytest.fixture
def env(tmp_path):
    keys = HubKeys.init(tmp_path / "hub")
    acc = Access(str(tmp_path / "access.db"))
    acc.add_user(ADMIN, "admin")
    acc.add_user(USER, "user")
    acc.add_member("hub-admin", ADMIN)
    events: list[dict] = []
    acc.observer = events.append
    yield keys, acc, events
    acc.close()


def test_new_hub_has_the_default_at_version_1(env):
    _k, acc, _e = env
    row = acc.market_policy()
    assert row["version"] == 1 and row["policy"] == MP.default_policy() and row["hash"] == MP.policy_hash(row["policy"])


def test_set_bumps_version_and_audits_in_the_same_step(env):
    _k, acc, events = env
    p = company_policy()
    out = acc.set_market_policy(1, p, actor=ADMIN)
    assert out["version"] == 2 and acc.market_policy() == out
    assert events == [{"kind": "market_policy", "version": 2, "policy_hash": MP.policy_hash(p), "actor_user_id": ADMIN}]


def test_stale_base_version_conflicts_and_nothing_changes(env):
    _k, acc, events = env
    acc.set_market_policy(1, company_policy(), actor=ADMIN)
    with pytest.raises(AccessError) as e:
        acc.set_market_policy(1, MP.default_policy(), actor=ADMIN)
    assert e.value.code == "conflict" and acc.market_policy()["version"] == 2 and len(events) == 1


@pytest.mark.parametrize("bad", [None, {}, {"markets": []}, {"allow": [], "block": [], "ceiling": []}])
def test_malformed_policy_is_refused(env, bad):
    _k, acc, events = env
    with pytest.raises(AccessError) as e:
        acc.set_market_policy(1, bad, actor=ADMIN)
    assert e.value.code == "malformed" and acc.market_policy()["version"] == 1 and not events


@pytest.mark.parametrize("base", [0, -1, True, "1", None])
def test_bad_base_version_is_malformed(env, base):
    _k, acc, _e = env
    with pytest.raises(AccessError) as e:
        acc.set_market_policy(base, MP.default_policy(), actor=ADMIN)
    assert e.value.code == "malformed"


def test_only_a_hub_admin_may_set_it(env):
    _k, acc, events = env
    with pytest.raises(AccessError) as e:
        acc.set_market_policy(1, company_policy(), actor=USER)
    assert e.value.code == "forbidden_role" and acc.market_policy()["version"] == 1 and not events


def test_a_failing_audit_refuses_the_change(env):
    _k, acc, _e = env

    def boom(_ev):
        raise OSError("audit.db is gone")

    acc.observer = boom
    with pytest.raises(OSError):
        acc.set_market_policy(1, company_policy(), actor=ADMIN)
    assert acc.market_policy()["version"] == 1 and acc.market_policy()["policy"] == MP.default_policy()


def test_without_an_audit_observer_the_change_is_refused(env):
    _k, acc, _e = env
    acc.observer = None
    with pytest.raises(AccessError):
        acc.set_market_policy(1, company_policy(), actor=ADMIN)
    assert acc.market_policy()["version"] == 1


def test_a_damaged_row_fails_closed(env):
    _k, acc, _e = env
    acc._db.execute("UPDATE market_policy SET policy='{\"markets\": []}' WHERE id=1")
    with pytest.raises(AccessError):
        acc.market_policy()


def _verify(keys, signed, pinned=None):
    held = H.verify_card(keys.card(), now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)
    return H.verify_snapshot(
        signed, held, workspace_id=WS, dev_mode=True, pinned=pinned, revoked_op_certs=keys.revoked_op_certs()
    )


def test_snapshot_carries_the_policy_and_its_version_rises_on_change(env):
    keys, acc, _e = env
    snaps = AccessSnapshots(acc, keys)
    s1 = snaps.snapshot(WS, "direct")
    o1 = _verify(keys, s1)
    assert o1["market_policy"] == MP.default_policy() and o1["version"] == 1
    assert snaps.snapshot(WS, "direct") == s1  # unchanged content: the stored bytes
    p = company_policy()
    acc.set_market_policy(1, p, actor=ADMIN)
    o2 = _verify(keys, snaps.snapshot(WS, "direct"), pinned=o1)
    assert o2["version"] == 2 and o2["market_policy"] == p
    assert snaps.snapshot("b2" * 16, "selective")["o"]["market_policy"] == p  # selective workspaces get it too


def test_a_snapshot_without_the_field_or_with_a_bad_one_is_malformed(env):
    keys, acc, _e = env
    snaps = AccessSnapshots(acc, keys)
    s = snaps.snapshot(WS, "direct")
    for mutate in (lambda o: o.pop("market_policy"), lambda o: o.update(market_policy=None)):
        o = json.loads(json.dumps(s["o"]))
        mutate(o)
        signed = keys.op_signer.sign_object(o)
        with pytest.raises(Refused) as e:
            _verify(keys, signed)
        assert e.value.code == "malformed"


@pytest.fixture
def w(tmp_path):
    x = Web(tmp_path)
    yield x
    x.close()


def test_admin_routes_get_put_conflict_and_non_admin(w):
    tok, ann = w.login(w.admin), w.login(w.ann)
    s, o, _ = w.call("GET", "/admin/market-policy", token=tok)
    assert s == 200 and o["version"] == 1 and o["policy"] == MP.default_policy()
    p = company_policy()
    s, o, _ = w.call("PUT", "/admin/market-policy", {"base_version": 1, "policy": p}, token=tok)
    assert s == 200 and o["version"] == 2 and o["hash"] == MP.policy_hash(p)
    assert w.call("PUT", "/admin/market-policy", {"base_version": 1, "policy": p}, token=tok)[:2] == (
        409,
        {"error": "conflict"},
    )
    assert w.call("PUT", "/admin/market-policy", {"base_version": 2, "policy": {"x": 1}}, token=tok)[:2] == (
        400,
        {"error": "malformed"},
    )
    assert w.call("PUT", "/admin/market-policy", {"base_version": "2", "policy": p}, token=tok)[:2] == (
        400,
        {"error": "malformed"},
    )
    assert w.call("GET", "/admin/market-policy", token=ann)[:2] == (403, {"error": "forbidden_role"})
    assert w.call("PUT", "/admin/market-policy", {"base_version": 2, "policy": p}, token=ann)[:2] == (
        403,
        {"error": "forbidden_role"},
    )
    assert w.call("GET", "/admin/market-policy", token=tok)[1]["version"] == 2


def test_the_change_is_in_the_audit_chain_as_a_hub_wide_record(w):
    tok = w.login(w.admin)
    p = company_policy()
    w.call("PUT", "/admin/market-policy", {"base_version": 1, "policy": p}, token=tok)
    _, o, _ = w.call("GET", "/admin/audit?since=0", token=tok)
    recs = [r for r in o["records"] if r["record"]["o"]["type"] == "market_policy"]
    assert len(recs) == 1 and recs[0]["record"]["o"]["scope_tag"] is None
    assert recs[0]["event"]["version"] == 2 and recs[0]["event"]["actor_user_id"] == w.admin
    assert recs[0]["event"]["policy_hash"] == MP.policy_hash(p)
