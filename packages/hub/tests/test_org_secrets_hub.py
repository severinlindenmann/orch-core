"""M3 hub secrets S-2 (#1431-#1433): the store, the admin routes, the signed ``hub.org_secrets`` bundle, the link ops and the
cleanup on unbind. Opus conditions 5-9 of ``state/devA/secrets-design.md`` section 6: each has a test below."""

# ruff: noqa: RUF059
from __future__ import annotations

import datetime
import logging
import sqlite3
from types import SimpleNamespace

import pytest
from conftest import Hub
from secrets_helpers import DAY, VALUE, WS_A, WS_B, WS_C, SWeb, join

from orch_hub.access import Access, AccessSnapshots
from orch_hub.access.orgsecrets import org_secrets
from orch_hub.access.store import AccessError
from orch_hub.activity.store import ActivityLog
from orch_hub.contract import hub as H
from orch_hub.contract import org_secrets as OS
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.contract.hub import Refused
from orch_hub.keys import HubKeys
from orch_hub.link import Registry
from orch_hub.link.ops import HANDLERS, org_secrets_get, secret_use


@pytest.fixture
def w(tmp_path, monkeypatch):
    web = SWeb(tmp_path, monkeypatch)
    web.api.d.activity = ActivityLog(":memory:")
    yield web
    web.close()


def create(w, name="crm-key", shared="all", workspaces=()):
    rec = w.recipients(name, shared, workspaces)
    return w.save(rec, shared, workspaces, name=name), rec


def stored(w):
    a = w.env.acc
    return {
        "secrets": a._q("SELECT id, name, version, shared, removed_ms FROM org_secrets ORDER BY name"),
        "shares": a._q("SELECT secret_id, workspace_id FROM org_secret_shares ORDER BY 1, 2"),
        "wraps": a._q("SELECT secret_id, version, workspace_id, wxk_version FROM org_secret_wraps ORDER BY 1, 3"),
    }


# --- 1. the store holds no value ---------------------------------------------------------------------------------------------


def test_no_column_holds_a_value_and_the_value_never_reaches_the_database(w, tmp_path):
    a = w.env.acc
    org_secrets(a)
    cols = {
        r[1]
        for t in ("org_secrets", "org_secret_shares", "org_secret_wraps", "org_secret_use")
        for r in a._db.execute(f"PRAGMA table_info({t})")
    }
    assert not cols & {"value", "plaintext", "secret_value", "secret", "password"}
    create(w)
    a._db.execute("PRAGMA wal_checkpoint(FULL)")
    raw = b"".join(
        p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and not p.name.endswith((".sock", ".pid"))
    )
    assert VALUE.encode() not in raw


# --- 2. the request schema has no value member ---------------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["value", "plaintext", "secret_value", "secret", "password", "token"])
def test_the_save_schema_refuses_any_value_like_member(w, field):
    rec = w.recipients()
    body = {**w.seal(rec), "name": "crm-key", "shared": "all", "workspaces": [], field: VALUE}
    s, o, _ = w.call("POST", "/admin/secrets", body, token=w.token)
    assert s == 400 and o["error"] == "malformed"
    assert stored(w)["secrets"] == []
    s, o, _ = w.call(
        "POST",
        "/admin/secrets/recipients",
        {"name": "crm-key", "shared": "all", "workspaces": [], field: VALUE},
        token=w.token,
    )
    assert s == 400
    s, o, _ = w.call(
        "PUT", "/admin/secrets/crm-key/share", {"shared": "all", "workspaces": [], field: VALUE}, token=w.token
    )
    assert s == 400


def test_a_wrap_with_a_value_member_is_refused_too(w):
    rec = w.recipients()
    body = {**w.seal(rec), "name": "crm-key", "shared": "all", "workspaces": []}
    body["wraps"][0]["value"] = VALUE
    s, o, _ = w.call("POST", "/admin/secrets", body, token=w.token)
    assert s == 400 and stored(w)["secrets"] == []


# --- create, list, open ----------------------------------------------------------------------------------------------------------


def test_create_stores_one_wrap_per_bound_workspace_and_it_opens_with_the_workspace_key(w):
    out, rec = create(w)
    assert out == {"name": "crm-key", "version": 1}
    assert {r["workspace_id"] for r in rec["recipients"]} == {WS_A, WS_B} and rec["version"] == 1
    got = {x[2]: x for x in stored(w)["wraps"]}
    assert set(got) == {WS_A, WS_B} and all(x[1] == 1 and x[3] == 1 for x in got.values())
    row = w.env.acc._q("SELECT sealed FROM org_secret_wraps WHERE workspace_id=?", (WS_A,))[0][0]
    exp = w.env.acc._q("SELECT expires_ms FROM org_secrets")[0][0]
    value = OS.open_org_secret(
        G.K.priv["wxk_a"], rec["hub_id"], WS_A, rec["secret_id"], 1, 1, exp, "crm-key", bytes(row), w.now, w.now
    )
    assert value == VALUE
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    sec = o["secrets"][0]
    assert s == 200 and sec["name"] == "crm-key" and sec["shared"] == "all"
    assert {k: v["state"] for k, v in sec["state"].items()} == {WS_A: "ready", WS_B: "ready"}
    assert "sealed" not in repr(o)
    assert all("owner_name" in x for x in o["workspaces"])  # P10: the page names a workspace by its owner, never an id


def test_secrets_list_workspaces_carry_the_owner_display_name(w):
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    assert s == 200 and o["workspaces"]
    names = {u["name"] for u in w.api.d.access.users()}
    for x in o["workspaces"]:
        assert x["owner_name"] is None or x["owner_name"] in names
        assert not any(k in x for k in ("owner_alias", "sealed", "value"))


def test_only_a_hub_admin_may_use_the_routes(w):
    ann = w.login(w.ann)
    for method, path, body in (
        ("GET", "/admin/secrets", None),
        ("POST", "/admin/secrets/recipients", {"name": "crm-key", "shared": "all", "workspaces": []}),
        ("POST", "/admin/secrets", {}),
        ("PUT", "/admin/secrets/crm-key/share", {"shared": "all", "workspaces": []}),
        ("DELETE", "/admin/secrets/crm-key", None),
    ):
        assert w.call(method, path, body, token=ann)[0] == 403, path
        assert w.call(method, path, body)[0] == 401, path
        if method != "GET":
            assert w.call(method, path, body, token=w.token, csrf=False)[0] == 403, path


# --- 5. versions: exactly current + 1, atomically ---------------------------------------------------------------------------------


def test_condition_5_version_must_be_exactly_current_plus_one(w):
    create(w)
    s0 = stored(w)
    rec = w.recipients()
    assert rec["version"] == 2
    for bad in (1, 3, 0, -1, "2", 2.0, True):
        body = {**w.seal(rec), "name": "crm-key", "shared": "all", "workspaces": [], "version": bad}
        s, o, _ = w.call("POST", "/admin/secrets", body, token=w.token)
        assert s in (400, 409), bad
    assert stored(w) == s0
    w.save(rec)
    assert stored(w)["secrets"][0][2] == 2
    # a replay of the same post is refused, nothing changes
    s1 = stored(w)
    body = {**w.seal(rec), "name": "crm-key", "shared": "all", "workspaces": []}
    s, o, _ = w.call("POST", "/admin/secrets", body, token=w.token)
    assert s == 409 and o["error"] == "conflict" and stored(w) == s1


def test_a_new_secret_must_be_version_one_and_a_fresh_id(w):
    rec = w.recipients()
    s, o, _ = w.call(
        "POST",
        "/admin/secrets",
        {**w.seal(rec), "version": 2, "name": "crm-key", "shared": "all", "workspaces": []},
        token=w.token,
    )
    assert s == 409
    create(w)
    other = w.recipients("other-key")
    s, o, _ = w.call(
        "POST",
        "/admin/secrets",
        {
            **w.seal(other, name="other-key"),
            "secret_id": stored(w)["secrets"][0][0],
            "name": "other-key",
            "shared": "all",
            "workspaces": [],
        },
        token=w.token,
    )
    assert s == 409  # an id that exists is never reused for another name
    # a rotation with another secret_id than the live one is refused
    rec = w.recipients()
    body = {**w.seal(rec), "secret_id": "ab" * 16, "name": "crm-key", "shared": "all", "workspaces": []}
    assert w.call("POST", "/admin/secrets", body, token=w.token)[0] == 409


def test_rotation_replaces_every_wrap_with_the_new_version(w):
    create(w)
    old = {r[2]: bytes(r[0]) for r in w.env.acc._q("SELECT sealed, 0, workspace_id FROM org_secret_wraps")}
    w.save(w.recipients())
    new = {r[2]: bytes(r[0]) for r in w.env.acc._q("SELECT sealed, 0, workspace_id FROM org_secret_wraps")}
    assert set(new) == set(old) and all(new[k] != old[k] for k in new)
    assert {x[1] for x in stored(w)["wraps"]} == {2}


# --- 6. the wrap set == the bound workspaces of the share set -------------------------------------------------------------------------


def test_condition_6_the_wraps_are_exactly_the_share_sets_bound_workspaces(w):
    rec = w.recipients()
    full = w.seal(rec)["wraps"]
    base = {
        "name": "crm-key",
        "shared": "all",
        "workspaces": [],
        "secret_id": rec["secret_id"],
        "version": 1,
        "expires_ms": w.now + 30 * DAY,
    }

    def post(wraps):
        return w.call("POST", "/admin/secrets", {**base, "wraps": wraps}, token=w.token)

    assert post(full[:1])[0] == 409  # a workspace without its wrap
    assert post([])[0] == 409
    s, o, _ = post(full + [full[0]])
    assert s == 400  # a duplicate
    other = {**full[0], "workspace_id": WS_C}  # a workspace that is not bound
    assert post(full + [other])[0] == 409  # an extra one
    assert post([{**full[0], "wxk_version": 2}, full[1]])[0] == 409  # not the verified card's version
    assert post([{**full[0], "sealed": "AAAA"}, full[1]])[0] == 400  # not a wrap
    assert post([{**full[0], "sealed": b64u(b"\x04" + b"\0" * 100)}, full[1]])[0] == 400  # not a point
    assert post([{"workspace_id": WS_A}, full[1]])[0] == 400
    assert stored(w)["secrets"] == [] and stored(w)["wraps"] == []
    assert post(full)[0] == 200


def test_chosen_share_needs_wraps_for_the_chosen_bound_workspaces_only(w):
    rec = w.recipients("crm-key", "chosen", [WS_A, WS_C])  # WS_C is not bound: no wrap, no recipient
    assert [r["workspace_id"] for r in rec["recipients"]] == [WS_A]
    w.save(rec, "chosen", [WS_A, WS_C])
    assert [x[2] for x in stored(w)["wraps"]] == [WS_A]
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    assert list(o["secrets"][0]["state"]) == [WS_A]


def test_a_workspace_that_joins_later_needs_the_value_again(w):
    create(w)
    join(w.registry, WS_C)
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    assert {k: v["state"] for k, v in o["secrets"][0]["state"].items()} == {
        WS_A: "ready",
        WS_B: "ready",
        WS_C: "needs_value",
    }
    rec = w.recipients()
    assert {r["workspace_id"] for r in rec["recipients"]} == {WS_A, WS_B, WS_C} and rec["version"] == 2
    w.save(rec)  # "Enter the value again": version + 1 for every current recipient
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    assert {v["state"] for v in o["secrets"][0]["state"].values()} == {"ready"}


def test_a_rotated_workspace_key_makes_its_wrap_stale_and_the_post_must_use_the_new_version(w):
    create(w)
    join(w.registry, WS_A, wxk_version=2)  # the workspace rotated its WXK (a new verified card)
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    assert (
        o["secrets"][0]["state"][WS_A]["state"] == "needs_value" and o["secrets"][0]["state"][WS_B]["state"] == "ready"
    )
    rec = w.recipients()
    assert {r["workspace_id"]: r["wxk_version"] for r in rec["recipients"]} == {WS_A: 2, WS_B: 1}
    stale = w.seal(rec)
    stale["wraps"][0]["wxk_version"] = 1
    s, o, _ = w.call(
        "POST", "/admin/secrets", {**stale, "name": "crm-key", "shared": "all", "workspaces": []}, token=w.token
    )
    assert s == 409
    w.save(rec)
    assert {x[2]: x[3] for x in stored(w)["wraps"]} == {WS_A: 2, WS_B: 1}


def test_recipients_come_only_from_the_verified_v2_card(w):
    # a workspace whose card does not verify gets no recipient entry; it is listed as without a key
    wsk, _ = ("wsk_a", None)
    bad = G.v2_card(ws=WS_C, wsk=wsk, wxk_pub=G.K.b("wxk_b"))
    bad["o"] = {**bad["o"], "wxk_pub": G.K.b("wxk_a")}  # the signed card no longer matches
    w.registry.commit_join(
        WS_C, G.P_ALICE, G.K.b(wsk), G.b64u(G.K.pub["pk_alice"]), bad, {"status": "bound", "binding": {"mode": "full"}}
    )
    rec = w.recipients()
    assert {r["workspace_id"] for r in rec["recipients"]} == {WS_A, WS_B} and rec["without_key"] == [WS_C]
    w.save(rec)  # the missing one does not block the others; it shows as needing the value
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    assert o["secrets"][0]["state"][WS_C]["state"] == "needs_value"
    # a key the browser claims is never used: the wraps are checked against the card's version only
    w.registry.commit_join(
        WS_C,
        G.P_ALICE,
        G.K.b(wsk),
        G.b64u(G.K.pub["pk_alice"]),
        {"o": {"wxk_pub": "x", "wxk_version": 1}},
        {"status": "bound", "binding": {"mode": "full"}},
    )
    assert WS_C not in {r["workspace_id"] for r in w.recipients()["recipients"]}


def test_expiry_is_required_and_at_most_a_year_ahead(w):
    rec = w.recipients()
    base = {**w.seal(rec), "name": "crm-key", "shared": "all", "workspaces": []}
    no_exp = {k: v for k, v in base.items() if k != "expires_ms"}
    assert w.call("POST", "/admin/secrets", no_exp, token=w.token)[0] == 400
    for bad in (None, 0, w.now, w.now - 1, w.now + 366 * DAY + 1, "x", 1.5, True):
        assert w.call("POST", "/admin/secrets", {**base, "expires_ms": bad}, token=w.token)[0] == 400, bad
    assert stored(w)["secrets"] == []
    assert (
        w.call(
            "POST",
            "/admin/secrets",
            {**w.seal(rec, expires_ms=w.now + 366 * DAY), "name": "crm-key", "shared": "all", "workspaces": []},
            token=w.token,
        )[0]
        == 200
    )


def test_names_are_unique_among_live_secrets_and_free_again_after_removal(w):
    create(w)
    s, o, _ = w.call("DELETE", "/admin/secrets/crm-key", token=w.token)
    assert s == 200
    rec = w.recipients()
    assert rec["version"] == 1 and rec["secret_id"] != stored(w)["secrets"][0][0]
    w.save(rec)
    assert [(r[1], r[4] is None) for r in stored(w)["secrets"]] == [("crm-key", False), ("crm-key", True)]
    for bad in ("Crm", "1a", "a" * 41, "a_b", ""):
        assert (
            w.call(
                "POST", "/admin/secrets/recipients", {"name": bad, "shared": "all", "workspaces": []}, token=w.token
            )[0]
            == 400
        )


# --- 7. one transaction each --------------------------------------------------------------------------------------------------------


def _fail_on_wrap_delete(a):
    a._db.execute("CREATE TEMP TRIGGER boom BEFORE DELETE ON org_secret_wraps BEGIN SELECT RAISE(ABORT, 'boom'); END")


def _fail_on_share_insert(a):
    a._db.execute("CREATE TEMP TRIGGER boom2 BEFORE INSERT ON org_secret_shares BEGIN SELECT RAISE(ABORT, 'boom'); END")


def test_condition_7_unshare_deletes_the_wraps_in_one_transaction(w):
    create(w, shared="chosen", workspaces=[WS_A, WS_B])
    before = stored(w)
    assert len(before["shares"]) == 2 and len(before["wraps"]) == 2
    _fail_on_wrap_delete(w.env.acc)  # the share rows are replaced first, the wrap delete is the second statement
    s, o, _ = w.call("PUT", "/admin/secrets/crm-key/share", {"shared": "chosen", "workspaces": [WS_A]}, token=w.token)
    assert s >= 400
    assert stored(w) == before  # nothing half done
    w.env.acc._db.execute("DROP TRIGGER boom")
    s, o, _ = w.call("PUT", "/admin/secrets/crm-key/share", {"shared": "chosen", "workspaces": [WS_A]}, token=w.token)
    assert s == 200
    after = stored(w)
    assert [x[1] for x in after["shares"]] == [WS_A] and [x[2] for x in after["wraps"]] == [WS_A]


def test_condition_7_remove_deletes_everything_in_one_transaction(w):
    create(w, shared="chosen", workspaces=[WS_A])
    before = stored(w)
    _fail_on_wrap_delete(w.env.acc)
    assert w.call("DELETE", "/admin/secrets/crm-key", token=w.token)[0] >= 400
    assert stored(w) == before
    w.env.acc._db.execute("DROP TRIGGER boom")
    assert w.call("DELETE", "/admin/secrets/crm-key", token=w.token)[0] == 200
    after = stored(w)
    assert after["wraps"] == [] and after["shares"] == [] and after["secrets"][0][4] is not None
    assert w.call("DELETE", "/admin/secrets/crm-key", token=w.token)[0] == 404


def test_condition_7_chosen_to_all_keeps_the_wraps(w):
    create(w, shared="chosen", workspaces=[WS_A])
    wraps = stored(w)["wraps"]
    s, o, _ = w.call("PUT", "/admin/secrets/crm-key/share", {"shared": "all", "workspaces": []}, token=w.token)
    assert (
        s == 200 and stored(w)["wraps"] == wraps and stored(w)["shares"] == [] and stored(w)["secrets"][0][3] == "all"
    )
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    st = o["secrets"][0]["state"]
    assert st[WS_A]["state"] == "ready" and st[WS_B]["state"] == "needs_value"  # the new one needs the value again


def test_condition_7_all_to_chosen_drops_the_wraps_of_the_others(w):
    create(w)
    assert (
        w.call("PUT", "/admin/secrets/crm-key/share", {"shared": "chosen", "workspaces": [WS_B]}, token=w.token)[0]
        == 200
    )
    assert [x[2] for x in stored(w)["wraps"]] == [WS_B]


def test_condition_7_a_failed_save_leaves_the_old_secret_untouched(w):
    create(w, shared="chosen", workspaces=[WS_A, WS_B])
    before = stored(w)
    _fail_on_share_insert(w.env.acc)  # the version bump and the wrap delete ran before the failing statement
    rec = w.recipients("crm-key", "chosen", [WS_A, WS_B])
    s, o, _ = w.call(
        "POST",
        "/admin/secrets",
        {**w.seal(rec), "name": "crm-key", "shared": "chosen", "workspaces": [WS_A, WS_B]},
        token=w.token,
    )
    assert s >= 400 and stored(w) == before


def test_condition_7_unbind_deletes_shares_wraps_and_counts_in_one_transaction(w):
    create(w, shared="chosen", workspaces=[WS_A, WS_B])
    store = org_secrets(w.env.acc)
    sid = stored(w)["secrets"][0][0]
    store.record_use(
        WS_A, sid, datetime.datetime.fromtimestamp(w.now / 1000, datetime.UTC).date().isoformat(), 3, w.now
    )
    before = stored(w)
    _fail_on_wrap_delete(w.env.acc)
    with pytest.raises(sqlite3.Error):
        store.drop_workspace(WS_A)
    assert stored(w) == before
    assert w.env.acc._q("SELECT COUNT(*) FROM org_secret_use")[0][0] == 1
    w.env.acc._db.execute("DROP TRIGGER boom")
    store.drop_workspace(WS_A)
    after = stored(w)
    assert [x[1] for x in after["shares"]] == [WS_B] and [x[2] for x in after["wraps"]] == [WS_B]
    assert w.env.acc._q("SELECT COUNT(*) FROM org_secret_use")[0][0] == 0


def test_a_workspace_leaving_through_the_link_drops_its_secrets():
    """binding_put with a signed ``hub.unbound`` runs the cleanup (the registry already holds the state)."""
    a = Access(":memory:")
    h = Hub(access=a)
    try:
        c = h.open(G.WS_A, "wsk_a", first=True)
        c.request(
            "join", {"invite": "inv1", "card": G.v2_card(), "pk_pub": b64u(G.K.pub["pk_alice"]), "binding": G.binding()}
        )
        store = org_secrets(a)
        with a.tx() as db:
            db.execute(
                "INSERT INTO org_secrets VALUES (?,?,?,?,?,?,?,NULL)",
                ("11" * 16, "crm-key", 1, G.NOW + DAY, "all", "aa" * 16, G.NOW),
            )
            db.execute("INSERT INTO org_secret_wraps VALUES (?,?,?,?,?)", ("11" * 16, 1, G.WS_A, 1, b"x" * 100))
            db.execute("INSERT INTO org_secret_wraps VALUES (?,?,?,?,?)", ("11" * 16, 1, G.WS_B, 1, b"x" * 100))
        assert c.request("binding_put", {"object": G.unbound(seq=2)}) == {}
        assert [r[0] for r in a._q("SELECT workspace_id FROM org_secret_wraps")] == [G.WS_B]
        assert store.get("crm-key")["id"] == "11" * 16  # the secret stays; only that workspace lost its wrap
    finally:
        h.close()
        a.close()


# --- the signed bundle and the link ops -----------------------------------------------------------------------------------------------------------


@pytest.fixture
def bundles(tmp_path):
    keys = HubKeys.init(tmp_path / "hub")
    acc = Access(str(tmp_path / "access.db"))
    t = [G.NOW]
    snaps = AccessSnapshots(acc, keys, lambda: t[0])
    reg = Registry()
    for ws in (WS_A, WS_B):
        join(reg, ws)
    yield SimpleNamespace(keys=keys, acc=acc, snaps=snaps, reg=reg, t=t, store=org_secrets(acc))
    reg.close()
    acc.close()


def _row(b, sid, name, ws, version=1, exp=None, shared="all", sealed=None, wxk=1):
    exp = exp or G.NOW + 30 * DAY
    with b.acc.tx() as db:
        if not db.execute("SELECT 1 FROM org_secrets WHERE id=?", (sid,)).fetchone():
            db.execute(
                "INSERT INTO org_secrets VALUES (?,?,?,?,?,?,?,NULL)",
                (sid, name, version, exp, shared, "aa" * 16, G.NOW),
            )
        db.execute(
            "INSERT OR REPLACE INTO org_secret_wraps VALUES (?,?,?,?,?)",
            (sid, version, ws, wxk, sealed or (b"\x04" + bytes(range(1, 65)) + b"c" * 30)),
        )


def _good_sealed(n=30):
    from cryptography.hazmat.primitives.asymmetric import ec

    from orch_hub.contract import suite as S

    return S.public_bytes(ec.generate_private_key(ec.SECP256R1())) + b"c" * n


def verify(b, signed, ws=WS_A, pinned=None):
    held = H.verify_card(b.keys.card(), now_ms=b.keys._now(), dev_mode=True, pinned_root=b.keys.hk_root_pub)
    return H.verify_org_secrets(
        signed, held, workspace_id=ws, dev_mode=True, pinned=pinned, revoked_op_certs=b.keys.revoked_op_certs()
    )


def ctx(b, ws):
    return SimpleNamespace(registry=b.reg, workspace_id=ws, snapshots=b.snaps, body={}, access=b.acc, now_ms=b.t[0])


def test_the_op_serves_only_the_link_workspaces_own_wraps(bundles):
    b = bundles
    _row(b, "11" * 16, "crm-key", WS_A, sealed=_good_sealed())
    _row(b, "11" * 16, "crm-key", WS_B, sealed=_good_sealed(40))
    _row(b, "22" * 16, "only-b", WS_B, sealed=_good_sealed())
    got_a = verify(b, org_secrets_get(ctx(b, WS_A))["bundle"])
    assert [s["name"] for s in got_a["secrets"]] == ["crm-key"]
    got_b = verify(b, org_secrets_get(ctx(b, WS_B))["bundle"], ws=WS_B)
    assert [s["name"] for s in got_b["secrets"]] == ["crm-key", "only-b"]
    # IDOR: the bytes of B's wraps are not in A's bundle, whatever A sends
    b_bytes = {s["sealed"] for s in got_b["secrets"]}
    assert not b_bytes & {s["sealed"] for s in got_a["secrets"]}
    forged = SimpleNamespace(**{**vars(ctx(b, WS_A)), "body": {"workspace_id": WS_B}})
    assert verify(b, org_secrets_get(forged)["bundle"])["workspace_id"] == WS_A
    with pytest.raises(Refused):
        org_secrets_get(ctx(b, WS_C))  # not pinned
    assert HANDLERS["org_secrets_get"] is org_secrets_get and H.LINK_OPS["org_secrets_get"] == ("w2h", None, {})
    assert got_a["secrets"][0]["id"] == "11" * 16 and set(got_a["secrets"][0]) == {
        "id",
        "name",
        "version",
        "expires_ms",
        "wxk_version",
        "sealed",
    }
    with pytest.raises(Refused):
        verify(b, org_secrets_get(ctx(b, WS_A))["bundle"], ws=WS_B)  # a host refuses another workspace's bundle


def test_the_bundle_leaves_out_removed_expired_and_unshared_secrets(bundles):
    b = bundles
    _row(b, "11" * 16, "live", WS_A, sealed=_good_sealed())
    _row(b, "22" * 16, "gone", WS_A, sealed=_good_sealed())
    _row(b, "33" * 16, "old", WS_A, exp=G.NOW + 5000, sealed=_good_sealed())
    _row(b, "44" * 16, "chosen-b", WS_A, shared="chosen", sealed=_good_sealed())  # a wrap but not in the share list
    b.acc._w("UPDATE org_secrets SET removed_ms=1 WHERE name='gone'")
    names = lambda: [s["name"] for s in verify(b, org_secrets_get(ctx(b, WS_A))["bundle"])["secrets"]]
    assert names() == ["live", "old"]
    b.t[0] += 10_000  # "old" has expired
    assert names() == ["live"]
    b.acc._w("INSERT INTO org_secret_shares VALUES ('44444444444444444444444444444444', ?)", (WS_A,))
    assert names() == ["chosen-b", "live"]


def test_versions_only_go_up_and_unchanged_content_is_byte_identical(bundles):
    b = bundles
    _row(b, "11" * 16, "live", WS_A, sealed=_good_sealed())
    one = org_secrets_get(ctx(b, WS_A))["bundle"]
    assert org_secrets_get(ctx(b, WS_A))["bundle"] == one and one["o"]["version"] == 1
    _row(b, "11" * 16, "live", WS_A, version=2, sealed=_good_sealed())
    b.acc._w("UPDATE org_secrets SET version=2")
    two = org_secrets_get(ctx(b, WS_A))["bundle"]
    assert two["o"]["version"] == 2
    with pytest.raises(Refused):
        verify(b, one, pinned=two["o"])  # a rollback
    assert verify(b, two, pinned=one["o"])["version"] == 2


def test_the_contract_refuses_a_bad_bundle_body():
    ok = {
        "id": "11" * 16,
        "name": "a-key",
        "version": 1,
        "expires_ms": G.NOW,
        "wxk_version": 1,
        "sealed": b64u(_good_sealed()),
    }
    assert OS.valid_org_secrets([ok])
    for bad in (
        [{**ok, "value": "x"}],
        [{**ok, "sealed": b64u(b"short")}],
        [{**ok, "sealed": b64u(b"\x05" + b"\0" * 100)}],
        [{**ok, "name": "A"}],
        [{**ok, "version": 0}],
        [{**ok, "id": "zz"}],
        [ok, ok],
        [{**ok, "name": "b-key"}, ok],
        "x",
    ):
        assert not OS.valid_org_secrets(bad), bad


# --- 4. the use log ---------------------------------------------------------------------------------------------------------------------------------


def test_secret_use_counts_are_bounded_rate_limited_and_only_for_shared_secrets(bundles):
    b = bundles
    _row(b, "11" * 16, "live", WS_A, sealed=_good_sealed())
    _row(b, "22" * 16, "chosen", WS_A, shared="chosen", sealed=_good_sealed())
    base = G.NOW // 86_400_000

    def iso(offset: int) -> str:
        return (datetime.date(1970, 1, 1) + datetime.timedelta(days=base + offset)).isoformat()

    day = iso(0)
    use = lambda sid, d, n, ws=WS_A: secret_use(
        SimpleNamespace(**{**vars(ctx(b, ws)), "body": {"secret_id": sid, "day": d, "n": n}})
    )
    assert use("11" * 16, day, 3) == {}
    assert use("11" * 16, day, 2) == {}  # the larger value is kept (a report is the day's total so far)
    assert use("11" * 16, iso(-1), 4) == {} and use("11" * 16, day, 5, ws=WS_B) == {}
    mine = {s["name"]: s["used_this_week"] for s in b.store.list(G.NOW)}
    assert mine["live"] == 12  # 3 + 4 (this workspace) + 5 (the other); sums are display data
    for sid, d, n in (
        ("11" * 16, iso(5), 1),
        ("11" * 16, iso(-30), 1),
        ("11" * 16, base, 1),
        ("11" * 16, "2026-13-40", 1),
        ("11" * 16, "20261029", 1),
        ("11" * 16, day, -1),
        ("11" * 16, day, 10**9),
        ("22" * 16, day, 1),
        ("99" * 16, day, 1),
    ):
        with pytest.raises(Refused):
            use(sid, d, n)  # out of range / not shared with that workspace / unknown
    assert H.LINK_OPS["secret_use"][0] == "w2h" and HANDLERS["secret_use"] is secret_use
    for k, bad in (
        ("n", "5"),
        ("n", 1.5),
        ("day", True),
        ("day", 20000),
        ("day", "2026-13-40"),
        ("day", "2026-1-1"),
        ("secret_id", "zz"),
    ):
        body = {"secret_id": "11" * 16, "day": day, "n": 1, k: bad}
        assert not all(chk(body[f]) for f, chk in H.LINK_OPS["secret_use"][2].items()), (k, bad)
    for i in range(200):
        try:
            use("11" * 16, day, 10 + i)
        except Refused as e:
            assert e.code == "rate_limited" and i >= 100
            break
    else:
        raise AssertionError("not rate limited")
    use("11" * 16, day, 7, ws=WS_B)  # another workspace has its own budget


def test_the_list_shows_use_per_workspace_as_untrusted_display_data(w):
    create(w)
    sid = stored(w)["secrets"][0][0]
    store = org_secrets(w.env.acc)
    store.record_use(
        WS_A, sid, datetime.datetime.fromtimestamp(w.now / 1000, datetime.UTC).date().isoformat(), 7, w.now
    )
    s, o, _ = w.call("GET", "/admin/secrets", token=w.token)
    sec = o["secrets"][0]
    assert (
        sec["used_this_week"] == 7
        and sec["state"][WS_A]["used_this_week"] == 7
        and sec["state"][WS_B]["used_this_week"] == 0
    )


# --- 9. activity and logs hold no wrap or value ---------------------------------------------------------------------------------------------------


def test_condition_9_activity_names_the_secret_and_never_a_wrap_or_value(w):
    log = w.api.d.activity
    out, rec = create(w)
    w.save(w.recipients())
    w.call("PUT", "/admin/secrets/crm-key/share", {"shared": "chosen", "workspaces": [WS_A]}, token=w.token)
    w.call("DELETE", "/admin/secrets/crm-key", token=w.token)
    events = log.list(limit=50)
    assert [e["type"] for e in events if e["type"].startswith("secret.")][::-1] == [
        "secret.created",
        "secret.rotated",
        "secret.shared",
        "secret.removed",
    ]
    assert all(e["detail"].get("secret") == "crm-key" for e in events if e["type"].startswith("secret."))
    text = repr(events)
    assert VALUE not in text and "sealed" not in text


def test_condition_9_request_logs_hold_no_wrap_or_value(w, caplog):
    caplog.set_level(logging.DEBUG)
    rec = w.recipients()
    body = {**w.seal(rec), "name": "crm-key", "shared": "all", "workspaces": []}
    wraps = [x["sealed"] for x in body["wraps"]]
    assert w.call("POST", "/admin/secrets", body, token=w.token)[0] == 200
    bad = {**body, "version": 7}
    assert w.call("POST", "/admin/secrets", bad, token=w.token)[0] == 409
    w.call("POST", "/admin/secrets", {**body, "value": VALUE}, token=w.token)
    w.call("GET", "/admin/secrets", token=w.token)
    logged = "\n".join(r.getMessage() + repr(r.args) + str(r.exc_info) for r in caplog.records)
    assert VALUE not in logged and not any(x in logged for x in wraps)
    # and no response echoes one back
    s, o, _ = w.call("POST", "/admin/secrets", {**body, "version": 7}, token=w.token)
    assert VALUE not in repr(o) and not any(x in repr(o) for x in wraps)
    for r in w.recipients()["recipients"]:
        assert VALUE not in repr(r)


# --- admin bits -----------------------------------------------------------------------------------------------------------------------------------------


def test_a_non_admin_store_call_is_refused(w):
    store = org_secrets(w.env.acc)
    with pytest.raises(AccessError) as e:
        store.remove("crm-key", actor="bb" * 16, now_ms=w.now)
    assert e.value.code == "forbidden_role"


def test_nothing_in_the_hub_source_can_log_a_wrap():
    """A grep test: the new modules never log, print or format a sealed value."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "orch_hub"
    for rel in ("access/orgsecrets.py", "webapi/secrets_api.py"):
        src = (root / rel).read_text()
        assert "logging" not in src and "print(" not in src and "getLogger" not in src, rel
