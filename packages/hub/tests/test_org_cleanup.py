"""S-2 review fix 2 and 3: what a workspace that unbinds or is removed must not keep (secrets, skills, tool servers) is dropped,
a failed drop is retried until done, and nothing is served to that workspace while a drop is pending."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import Hub
from secrets_helpers import WS_A, WS_B, SWeb

from orch_hub.access import Access
from orch_hub.access.orgcleanup import org_cleanup
from orch_hub.access.orgmcp import OrgMcpStore, org_mcp
from orch_hub.access.orgsecrets import OrgSecretStore
from orch_hub.access.orgskills import OrgSkillStore, org_skills
from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u
from orch_hub.contract.hub import Refused
from orch_hub.link.ops import mcp_servers_get, org_secrets_get, org_skills_get

ADMIN = "aa" * 16


def seed(a: Access, ws: str) -> None:
    org_cleanup(a)  # creates the tables
    a.add_user(ADMIN, "root")
    a.add_member("hub-admin", ADMIN)
    with a.tx() as db:
        db.execute(
            "INSERT INTO org_secrets VALUES (?,?,?,?,?,?,?,NULL)",
            ("11" * 16, "crm-key", 1, G.NOW + 10**9, "all", ADMIN, G.NOW),
        )
        db.execute("INSERT INTO org_secret_wraps VALUES (?,?,?,?,?)", ("11" * 16, 1, ws, 1, b"x" * 100))
    org_skills(a).put("invoices", "Write invoice numbers as INV-1.", actor=ADMIN)
    org_skills(a).set_offers("invoices", [ws], actor=ADMIN)
    org_mcp(a).put("crm", "https://tools.example.com/mcp", "none", None, "Our CRM", actor=ADMIN)
    org_mcp(a).set_offers("crm", [ws], actor=ADMIN)


def left(a: Access, ws: str) -> dict:
    q = a._q
    return {
        "wraps": q("SELECT 1 FROM org_secret_wraps WHERE workspace_id=?", (ws,)),
        "skills": q("SELECT 1 FROM org_skill_offer WHERE workspace_id=?", (ws,)),
        "mcp": q("SELECT 1 FROM org_mcp_offer WHERE workspace_id=?", (ws,)),
    }


def boom(self, workspace_id):
    raise RuntimeError("disk")


def test_unbind_drops_secrets_skills_and_tool_servers_through_the_link():
    a = Access(":memory:")
    h = Hub(access=a)
    try:
        c = h.open(G.WS_A, "wsk_a", first=True)
        c.request(
            "join", {"invite": "inv1", "card": G.v2_card(), "pk_pub": b64u(G.K.pub["pk_alice"]), "binding": G.binding()}
        )
        seed(a, G.WS_A)
        assert all(left(a, G.WS_A).values())
        assert c.request("binding_put", {"object": G.unbound(seq=2)}) == {}
        assert not any(left(a, G.WS_A).values()) and not org_cleanup(a).is_pending(G.WS_A)
    finally:
        h.close()
        a.close()


def test_a_failed_drop_stays_pending_serves_nothing_and_is_retried(monkeypatch):
    a = Access(":memory:")
    h = Hub(access=a)
    try:
        c = h.open(G.WS_A, "wsk_a", first=True)
        c.request(
            "join", {"invite": "inv1", "card": G.v2_card(), "pk_pub": b64u(G.K.pub["pk_alice"]), "binding": G.binding()}
        )
        seed(a, G.WS_A)
        with monkeypatch.context() as m:
            m.setattr(OrgSecretStore, "drop_workspace", boom)
            assert c.request("binding_put", {"object": G.unbound(seq=2)}) == {}
            assert left(a, G.WS_A)["wraps"] and org_cleanup(a).is_pending(G.WS_A)
            # skills and tool servers were still dropped: one failing drop never skips the others
            assert not left(a, G.WS_A)["skills"] and not left(a, G.WS_A)["mcp"]
            # the workspace joins again while the drop keeps failing: a pending drop is never cleared by a failure
            c.request("binding_put", {"object": G.binding(seq=3)})
            assert org_cleanup(a).is_pending(G.WS_A) and left(a, G.WS_A)["wraps"]
            reg = SimpleNamespace(
                lock=h.registry.lock, pin=h.registry.pin, binding_state=h.registry.binding_state, mode=h.registry.mode
            )
            ctx = SimpleNamespace(
                registry=reg,
                workspace_id=G.WS_A,
                snapshots=SimpleNamespace(
                    org_secrets=lambda *x: pytest.fail("served"),
                    org_skills=lambda *x: pytest.fail("served"),
                    mcp_servers=lambda *x: pytest.fail("served"),
                ),
                body={},
                access=a,
            )
            for op in (org_secrets_get, org_skills_get, mcp_servers_get):
                with pytest.raises(Refused) as e:
                    op(ctx)
                assert e.value.code == "unavailable"
            org_cleanup(a).retry()  # still failing: still pending
            assert org_cleanup(a).is_pending(G.WS_A)
        org_cleanup(a).retry()  # the store is back
        assert not org_cleanup(a).is_pending(G.WS_A) and not any(left(a, G.WS_A).values())
        assert a._q("SELECT * FROM org_cleanup_pending") == []
    finally:
        h.close()
        a.close()


def test_the_pending_mark_survives_a_restart(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    a = Access(path)
    seed(a, WS_A)
    monkeypatch.setattr(OrgSecretStore, "drop_workspace", boom)
    assert org_cleanup(a).drop(WS_A) is False
    a.close()
    monkeypatch.undo()
    a = Access(path)
    try:
        assert org_cleanup(a).is_pending(WS_A)
        org_cleanup(a).retry()
        assert not org_cleanup(a).is_pending(WS_A) and not left(a, WS_A)["wraps"]
    finally:
        a.close()


def test_removing_a_workspace_drops_its_company_content(tmp_path, monkeypatch):
    w = SWeb(tmp_path, monkeypatch)
    try:
        seed(w.env.acc, WS_B)
        w.registry.set_binding_state(WS_B, {"status": "unbound", "binding": {"mode": "full"}})
        s, o, _ = w.call("DELETE", f"/admin/workspaces/{WS_B}", token=w.token)
        assert s == 200, o
        assert not any(left(w.env.acc, WS_B).values())
    finally:
        w.close()


def test_skills_and_servers_drops_are_called_by_the_cleanup(monkeypatch):
    a = Access(":memory:")
    calls = []
    monkeypatch.setattr(OrgSkillStore, "drop_workspace", lambda self, ws: calls.append(("skills", ws)))
    monkeypatch.setattr(OrgMcpStore, "drop_workspace", lambda self, ws: calls.append(("mcp", ws)))
    try:
        assert org_cleanup(a).drop(WS_A) is True
        assert calls == [("skills", WS_A), ("mcp", WS_A)]
    finally:
        a.close()
