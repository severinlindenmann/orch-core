"""M3-4: remote MCP servers (#1435 #1436): the store, the signed ``hub.mcp_servers`` bundle, the op and the admin routes.

Decisions and conditions: remote only in v1 (an https address; there is no field a command or an environment could hide in),
no credential in the address (a secret is a NAME the secrets slice resolves), 1 signed by HK_op naming hub and workspace with a
monotonic version, 3 only the link's workspace gets its own servers, every act writes an activity record that names the
server and never its address."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from webapi_helpers import WS, Web

from orch_hub.access import Access, AccessSnapshots
from orch_hub.access.orgmcp import org_mcp
from orch_hub.access.store import AccessError
from orch_hub.activity.store import ActivityLog
from orch_hub.contract import hub as H
from orch_hub.contract import org_bundles as OB
from orch_hub.contract.canon import cj
from orch_hub.contract.hub import Refused
from orch_hub.keys import HubKeys
from orch_hub.link import Registry
from orch_hub.link.ops import HANDLERS, mcp_servers_get

ADMIN = "aa" * 16
WS2 = "b2" * 16
URL = "https://tools.example.com/mcp"


@pytest.fixture
def env(tmp_path):
    keys = HubKeys.init(tmp_path / "hub")
    acc = Access(str(tmp_path / "access.db"))
    acc.observer = lambda ev: None
    acc.add_user(ADMIN, "root")
    acc.add_member("hub-admin", ADMIN)
    acc.add_user("bb" * 16, "bob")
    t = [1_000_000]

    def clock():
        t[0] += 1000
        return t[0]

    yield keys, acc, AccessSnapshots(acc, keys, clock), org_mcp(acc)
    acc.close()


def held(keys):
    return H.verify_card(keys.card(), now_ms=keys._now(), dev_mode=True, pinned_root=keys.hk_root_pub)


def verify(keys, signed, ws=WS, pinned=None):
    return H.verify_org_mcp(
        signed, held(keys), workspace_id=ws, dev_mode=True, pinned=pinned, revoked_op_certs=keys.revoked_op_certs()
    )


def entry(**kw):
    return {"name": "crm", "url": URL, "auth": "none", "secret": None, "description": "Our CRM", **kw}


# --- remote only, no credential in the address ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "problem"),
    [
        ({}, None),
        ({"url": "https://tools.example.com:8443/a/b"}, None),
        ({"url": "https://8.8.8.8/mcp"}, None),  # a global address
        ({"url": "https://[2001:4860:4860::8888]/mcp"}, None),
        ({"url": "https://tools.example.com.cn/mcp"}, None),
        ({"url": "https://xn--bcher-kva.example/mcp"}, None),
        ({"url": "http://tools.example.com/mcp"}, "https_only"),
        ({"url": "stdio://npx some-server"}, "https_only"),
        ({"url": "npx -y some-server"}, "https_only"),
        ({"url": "https://tools.example.com/mcp?key=abc"}, "url"),  # a key never goes into an address
        ({"url": "https://tools.example.com/mcp#x"}, "url"),
        ({"url": "https://user:pw@tools.example.com/"}, "url"),
        ({"url": "https://tools.example.com/a b"}, "url"),
        ({"url": "https://tools.example.com:99999/"}, "url"),
        ({"url": "https://"}, "url"),
        ({"url": "https://" + "a" * 200 + ".com"}, "url"),
        ({"url": "https://localhost/mcp"}, "not_remote"),
        ({"url": "https://app.localhost/mcp"}, "not_remote"),
        ({"url": "https://127.0.0.1/mcp"}, "not_remote"),
        ({"url": "https://[::1]/mcp"}, "not_remote"),
        ({"url": "https://0.0.0.0/mcp"}, "not_remote"),
        ({"url": "https://169.254.169.254/latest"}, "not_remote"),  # the cloud metadata address
        ({"url": "https://intranet/mcp"}, "not_remote"),
        # private and shared ranges: only a global address is remote
        ({"url": "https://10.0.0.5/mcp"}, "not_remote"),
        ({"url": "https://192.168.1.1/mcp"}, "not_remote"),
        ({"url": "https://172.16.0.1/mcp"}, "not_remote"),
        ({"url": "https://172.31.255.254/mcp"}, "not_remote"),
        ({"url": "https://100.64.0.1/mcp"}, "not_remote"),  # the VPN / carrier-grade NAT range
        ({"url": "https://203.0.113.9/mcp"}, "not_remote"),  # documentation range
        ({"url": "https://[fd00::1]/mcp"}, "not_remote"),
        ({"url": "https://[fe80::1]/mcp"}, "not_remote"),
        ({"url": "https://[::ffff:10.0.0.1]/mcp"}, "not_remote"),
        ({"url": "https://[::ffff:127.0.0.1]/mcp"}, "not_remote"),
        # numbers spelled so a resolver reads them as an address
        ({"url": "https://127.1/mcp"}, "not_remote"),
        ({"url": "https://0x7f.0.0.1/mcp"}, "not_remote"),
        ({"url": "https://0177.0.0.1/mcp"}, "not_remote"),
        ({"url": "https://2130706433/mcp"}, "not_remote"),
        ({"url": "https://1.2.3.4.5/mcp"}, "not_remote"),
        ({"url": "https://example.123/mcp"}, "not_remote"),
        ({"url": "https://example.0x1/mcp"}, "not_remote"),
        # names that only mean something inside one network
        ({"url": "https://printer.local/mcp"}, "not_remote"),
        ({"url": "https://tools.internal/mcp"}, "not_remote"),
        ({"url": "https://nas.lan/mcp"}, "not_remote"),
        ({"url": "https://router.home/mcp"}, "not_remote"),
        ({"url": "https://crm.corp/mcp"}, "not_remote"),
        ({"name": "orch-tools"}, "reserved_name"),
        ({"name": "CRM"}, "name"),
        ({"auth": "oauth"}, "auth"),
        ({"auth": "hub_secret"}, "secret"),  # needs the name of a secret
        ({"auth": "none", "secret": "crm-key"}, "secret"),
        ({"auth": "hub_secret", "secret": "Crm Key"}, "secret"),
        ({"auth": "hub_secret", "secret": "crm-key"}, None),
        ({"description": "x" * 201}, "description"),
        ({"description": "two\nlines"}, "description"),
        ({"description": "ok" + chr(0xE0041)}, "description"),  # hidden characters: the same allowlist as skills
    ],
)
def test_the_server_rules(kw, problem):
    assert OB.mcp_problem(entry(**kw)) == problem


def test_the_entry_has_no_room_for_a_command():
    good = {**entry(), "digest": OB.mcp_digest(entry())}
    assert OB.valid_org_mcp([good])
    for extra in ("command", "args", "env", "headers"):
        assert not OB.valid_org_mcp([{**good, extra: "x"}])
    assert OB.mcp_problem({"name": "crm", "command": "npx"}) == "shape"


def test_store_validates_is_admin_only_capped_and_digests_follow_content(env):
    _k, _acc, _s, store = env
    with pytest.raises(AccessError) as e:
        store.put("crm", URL, "none", None, "", actor="bb" * 16)
    assert e.value.code == "forbidden_role"
    for args in (("crm", "http://x.example.com", "none", None, ""), ("orch-x", URL, "none", None, "")):
        with pytest.raises(AccessError):
            store.put(*args, actor=ADMIN)
    d1 = store.put("crm", URL, "none", None, "Our CRM", actor=ADMIN)["digest"]
    d2 = store.put("crm", URL + "/v2", "none", None, "Our CRM", actor=ADMIN)["digest"]
    d3 = store.put("crm", URL + "/v2", "hub_secret", "crm-key", "Our CRM", actor=ADMIN)["digest"]
    assert len({d1, d2, d3}) == 3  # a new address or a new sign-in is a new digest: each owner accepts again
    for n in range(OB.MCP_MAX - 1):
        store.put(f"srv-{n:02d}", URL, "none", None, "", actor=ADMIN)
    with pytest.raises(AccessError) as e:
        store.put("one-too-many", URL, "none", None, "", actor=ADMIN)
    assert e.value.code == "too_many"


# --- 1 and 3: the signed bundle -----------------------------------------------------------------------------------------------


def test_a_bundle_carries_only_the_servers_offered_to_that_workspace(env):
    keys, _acc, snaps, store = env
    store.put("crm", URL, "hub_secret", "crm-key", "Our CRM", actor=ADMIN)
    store.put("wiki", "https://wiki.example.com/mcp", "none", None, "", actor=ADMIN)
    store.set_share("crm", "chosen", [WS], actor=ADMIN)
    store.set_share("wiki", "chosen", [WS2], actor=ADMIN)
    o1 = verify(keys, snaps.mcp_servers(WS, "full"))
    o2 = verify(keys, snaps.mcp_servers(WS2, "full"), ws=WS2)
    assert [s["name"] for s in o1["servers"]] == ["crm"] and [s["name"] for s in o2["servers"]] == ["wiki"]
    assert o1["servers"][0]["secret"] == "crm-key" and "crm-key" not in repr(o2)  # a NAME, never a value
    assert o1["kind"] == "hub.mcp_servers" and o1["workspace_id"] == WS and o1["version"] == 1
    assert verify(keys, snaps.mcp_servers("c3" * 16, "full"), ws="c3" * 16)["servers"] == []


def test_versions_only_go_up_and_a_skills_bundle_is_not_a_servers_bundle(env):
    keys, _acc, snaps, store = env
    store.put("crm", URL, "none", None, "", actor=ADMIN)
    store.set_share("crm", "chosen", [WS], actor=ADMIN)
    a = snaps.mcp_servers(WS, "full")
    assert cj(snaps.mcp_servers(WS, "full")) == cj(a)
    store.put("crm", URL + "/v2", "none", None, "", actor=ADMIN)
    b = snaps.mcp_servers(WS, "full")
    assert verify(keys, b, pinned=verify(keys, a))["version"] == 2
    with pytest.raises(Refused) as e:
        verify(keys, a, pinned=verify(keys, b))
    assert e.value.code == "stale_bundle"
    with pytest.raises(Refused) as e:
        verify(keys, a, ws=WS2)
    assert e.value.code == "other_workspace"
    # the two bundle kinds have their own labels and verifiers: neither passes as the other
    with pytest.raises(Refused):
        H.verify_org_skills(a, held(keys), workspace_id=WS, dev_mode=True, pinned=None)
    skills = snaps.org_skills(WS, "full")
    with pytest.raises(Refused):
        verify(keys, skills)
    with pytest.raises(Refused):
        verify(keys, {"o": {**b["o"], "servers": []}, "sig": b["sig"]})
    assert "sig_hub_mcp_servers" in H.LABEL_OF_KIND.values()


def test_delete_and_unbind_take_a_server_out_of_every_bundle(env):
    keys, _acc, snaps, store = env
    store.put("crm", URL, "none", None, "", actor=ADMIN)
    store.set_share("crm", "chosen", [WS, WS2], actor=ADMIN)
    store.drop_workspace(WS)
    assert verify(keys, snaps.mcp_servers(WS, "full"))["servers"] == []
    assert [s["name"] for s in verify(keys, snaps.mcp_servers(WS2, "full"), ws=WS2)["servers"]] == ["crm"]
    store.delete("crm", actor=ADMIN)
    assert verify(keys, snaps.mcp_servers(WS2, "full"), ws=WS2)["servers"] == []
    with pytest.raises(AccessError) as e:
        store.delete("crm", actor=ADMIN)
    assert e.value.code == "unknown_server"


# --- the op ----------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def reg():
    r = Registry()
    for ws in (WS, WS2):
        r.commit_join(
            ws, ws, "key", "pk", {"name": ws}, {"status": "bound", "binding": {"mode": "full"}}, owner_alias=None
        )
    yield r
    r.close()


def test_the_op_serves_the_link_workspace_only(env, reg):
    keys, _acc, snaps, store = env
    store.put("crm", URL, "none", None, "", actor=ADMIN)
    store.set_share("crm", "chosen", [WS], actor=ADMIN)

    def c(ws):
        return SimpleNamespace(registry=reg, workspace_id=ws, snapshots=snaps, body={})

    assert [s["name"] for s in verify(keys, mcp_servers_get(c(WS))["bundle"])["servers"]] == ["crm"]
    assert verify(keys, mcp_servers_get(c(WS2))["bundle"], ws=WS2)["servers"] == []
    with pytest.raises(Refused):
        mcp_servers_get(c("c3" * 16))  # not pinned
    assert H.LINK_OPS["mcp_servers_get"] == ("w2h", None, {}) and "mcp_servers_get" in HANDLERS


# --- the admin routes ---------------------------------------------------------------------------------------------------------------


def test_the_admin_routes_save_offer_remove_audit_and_are_admin_only(tmp_path):
    w = Web(tmp_path)
    log = w.api.d.activity = ActivityLog(":memory:")
    try:
        admin, ann = w.login(w.admin), w.login(w.ann)
        w.reg.add(WS, "full", name="Product")
        body = {"url": URL, "auth": "hub_secret", "secret": "crm-key", "description": "Our CRM"}
        s, o, _ = w.call("PUT", "/admin/mcp-servers/crm", {**body, "url": "http://x.example.com"}, token=admin)
        assert s == 400 and o["error"] == "malformed"
        s, o, _ = w.call("PUT", "/admin/mcp-servers/crm", body, token=admin)
        assert s == 200 and o["name"] == "crm"
        s, o, _ = w.call("PUT", "/admin/mcp-servers/crm/offers", {"shared": "chosen", "workspaces": [WS]}, token=admin)
        assert s == 200 and o["offered_to"] == [WS]
        s, o, _ = w.call("GET", "/admin/mcp-servers", token=admin)
        assert s == 200 and o["servers"][0]["url"] == URL and o["servers"][0]["secret"] == "crm-key"
        assert any(x["workspace_id"] == WS for x in o["workspaces"])
        s, o, _ = w.call("PUT", "/admin/mcp-servers/ghost/offers", {"shared": "chosen", "workspaces": []}, token=admin)
        assert s == 404 and o["error"] == "unknown_server"
        for method, path, b in (
            ("GET", "/admin/mcp-servers", None),
            ("PUT", "/admin/mcp-servers/crm", body),
            ("PUT", "/admin/mcp-servers/crm/offers", {"shared": "chosen", "workspaces": []}),
            ("DELETE", "/admin/mcp-servers/crm", None),
        ):
            assert w.call(method, path, b, token=ann)[0] == 403, path
            assert w.call(method, path, b)[0] == 401, path
        assert w.call("DELETE", "/admin/mcp-servers/crm", token=admin)[0] == 200
        events = log.list(limit=50)
        assert {"mcp.saved", "mcp.offered", "mcp.removed"} <= {e["type"] for e in events}
        assert all(e["detail"].get("server") == "crm" for e in events if e["type"].startswith("mcp."))
        assert "tools.example.com" not in repr(events) and "crm-key" not in repr(events)  # the name, never the address
    finally:
        w.close()


# --- #1432: "all workspaces" is the default and is resolved when the bundle is built ------------------------------------------------


def test_all_is_the_default_and_covers_a_workspace_bound_later(env, reg):
    keys, _acc, snaps, store = env
    store.put("crm", URL, "none", None, "Our CRM", actor=ADMIN)
    assert store.list()[0]["shared"] == "all" and store.list()[0]["offered_to"] == []
    c = lambda ws: SimpleNamespace(registry=reg, workspace_id=ws, snapshots=snaps, body={})
    assert [s["name"] for s in verify(keys, mcp_servers_get(c(WS))["bundle"])["servers"]] == ["crm"]
    late = "c3" * 16
    reg.commit_join(
        late, late, "key", "pk", {"name": "late"}, {"status": "bound", "binding": {"mode": "full"}}, owner_alias=None
    )
    assert [s["name"] for s in verify(keys, mcp_servers_get(c(late))["bundle"], ws=late)["servers"]] == ["crm"]


def test_all_stops_at_unbind_and_chosen_narrows_again(env, reg):
    keys, _acc, snaps, store = env
    store.put("crm", URL, "none", None, "Our CRM", actor=ADMIN)
    c = lambda ws: SimpleNamespace(registry=reg, workspace_id=ws, snapshots=snaps, body={})
    reg.set_binding_state(WS, {"status": "unbound", "binding": {"mode": "full"}})
    with pytest.raises(Refused):
        mcp_servers_get(c(WS))
    store.set_share("crm", "chosen", [], actor=ADMIN)
    assert verify(keys, snaps.mcp_servers(WS2, "full"), ws=WS2)["servers"] == []
    for bad in ("some", None):
        with pytest.raises(AccessError):
            store.set_share("crm", bad, [], actor=ADMIN)


def test_a_store_made_before_shared_existed_gets_the_column(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript(
        "CREATE TABLE org_mcp (name TEXT PRIMARY KEY, url TEXT NOT NULL, auth TEXT NOT NULL, secret TEXT, description TEXT NOT NULL, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL);"
        "CREATE TABLE org_mcp_offer (name TEXT NOT NULL REFERENCES org_mcp(name) ON DELETE CASCADE, workspace_id TEXT NOT NULL, PRIMARY KEY (name, workspace_id));"
        f"INSERT INTO org_mcp VALUES ('old','{URL}','none',NULL,'',1,1); INSERT INTO org_mcp_offer VALUES ('old','{WS}');"
    )
    c.commit()
    c.close()
    db.chmod(0o600)
    acc = Access(str(db))
    try:
        acc.add_user(ADMIN, "root")
        acc.add_member("hub-admin", ADMIN)
        s = org_mcp(acc)
        assert [(x["name"], x["shared"], x["offered_to"]) for x in s.list()] == [("old", "chosen", [WS])]
        s.put("new", URL, "none", None, "", actor=ADMIN)
        assert {x["name"]: x["shared"] for x in s.list()} == {"old": "chosen", "new": "all"}
    finally:
        acc.close()


def test_narrowing_drops_a_pending_restore_click(env):
    _keys, acc, _snaps, store = env
    from orch_hub.backup import guard as GD

    store.put("crm", URL, "none", None, "", actor=ADMIN)
    with acc.tx() as db:
        db.execute(GD.PENDING_SCHEMA)
        db.execute("INSERT INTO restore_pending(kind,a,label) VALUES ('mcp_all','crm','crm')")
    store.set_share("crm", "chosen", [], actor=ADMIN)
    assert GD.pending(acc) == []
