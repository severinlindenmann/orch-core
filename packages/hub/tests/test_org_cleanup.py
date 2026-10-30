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
from orch_hub.link import Registry
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
    org_skills(a).set_share("invoices", "chosen", [ws], actor=ADMIN)
    org_mcp(a).put("crm", "https://tools.example.com/mcp", "none", None, "Our CRM", actor=ADMIN)
    org_mcp(a).set_share("crm", "chosen", [ws], actor=ADMIN)


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


# --- Opus delta on the fixes: M, L1, L2 ---------------------------------------------------------------------------------------


def _joined(h, a):
    c = h.open(G.WS_A, "wsk_a", first=True)
    c.request(
        "join", {"invite": "inv1", "card": G.v2_card(), "pk_pub": b64u(G.K.pub["pk_alice"]), "binding": G.binding()}
    )
    seed(a, G.WS_A)
    return c


def _ctx(h, a, **snap):
    reg = SimpleNamespace(
        lock=h.registry.lock, pin=h.registry.pin, binding_state=h.registry.binding_state, mode=h.registry.mode
    )
    return SimpleNamespace(
        registry=reg, workspace_id=G.WS_A, snapshots=SimpleNamespace(**snap), body={}, access=a, now_ms=G.NOW
    )


def test_M_the_pending_mark_is_written_before_the_registry_commit_and_survives_a_crash_after_it(tmp_path, monkeypatch):
    path = str(tmp_path / "a.db")
    a = Access(path)
    h = Hub(access=a)
    try:
        c = _joined(h, a)
        real = Registry.set_binding_state

        def commit_then_crash(self, ws, state, obj=None):
            real(self, ws, state, obj)
            raise RuntimeError("crash right after the commit")

        monkeypatch.setattr(Registry, "set_binding_state", commit_then_crash)
        with pytest.raises(Exception):  # noqa: B017 - the link reports the failed op
            c.request("binding_put", {"object": G.unbound(seq=2)})
        monkeypatch.undo()
        assert h.registry.binding_state(G.WS_A)["status"] == "unbound"  # committed, but the drop never ran
        assert left(a, G.WS_A)["wraps"] and org_cleanup(a).is_pending(G.WS_A)
    finally:
        h.close()
        a.close()
    a = Access(path)  # restart: the mark is on disk
    try:
        assert org_cleanup(a).is_pending(G.WS_A)
        org_cleanup(a).retry()
        assert not org_cleanup(a).is_pending(G.WS_A) and not any(left(a, G.WS_A).values())
    finally:
        a.close()


def test_M_between_the_commit_and_the_drop_nothing_is_served(monkeypatch):
    """On a rejoin the old wraps must not be served in the gap before the drop runs: the mark is already set when it starts."""
    from orch_hub.access.orgcleanup import OrgCleanup

    a = Access(":memory:")
    h = Hub(access=a)
    seen = []
    try:
        c = _joined(h, a)
        c.request("binding_put", {"object": G.unbound(seq=2)})
        seed_again = lambda: None
        seed_again()
        real = OrgCleanup.drop

        def spy(self, ws):
            seen.append(self.is_pending(ws))
            return real(self, ws)

        monkeypatch.setattr(OrgCleanup, "drop", spy)
        c.request("binding_put", {"object": G.binding(seq=3)})  # a fresh join: held status was unbound
        assert seen == [True]
    finally:
        h.close()
        a.close()


def test_L1_the_gate_is_checked_again_after_the_fetch():
    a = Access(":memory:")
    h = Hub(access=a)
    try:
        _joined(h, a)

        def fetch_while_it_leaves(*_):
            org_cleanup(a).mark(G.WS_A)  # a concurrent unbind marks it while the bundle is being built
            return {"o": {}}

        ctx = _ctx(
            h, a, org_secrets=fetch_while_it_leaves, org_skills=fetch_while_it_leaves, mcp_servers=fetch_while_it_leaves
        )
        for op in (org_secrets_get, org_skills_get, mcp_servers_get):
            org_cleanup(a).drop(G.WS_A)
            with pytest.raises(Refused) as e:
                op(ctx)
            assert e.value.code == "unavailable"
        # and a workspace that became unbound during the fetch gets nothing either
        org_cleanup(a).drop(G.WS_A)
        h.registry.set_binding_state(G.WS_A, {"status": "unbound", "binding": {"mode": "full"}})
        ctx2 = _ctx(h, a, org_secrets=lambda *_: {"o": {}})
        h.registry.set_binding_state(G.WS_A, {"status": "bound", "binding": {"mode": "full"}})

        def unbind_during(*_):
            h.registry.set_binding_state(G.WS_A, {"status": "unbound", "binding": {"mode": "full"}})
            return {"o": {}}

        ctx2.snapshots.org_secrets = unbind_during
        with pytest.raises(Refused):
            org_secrets_get(ctx2)
    finally:
        h.close()
        a.close()


def test_L2_secret_use_is_not_accepted_while_a_drop_is_pending():
    from orch_hub.link.ops import secret_use

    a = Access(":memory:")
    h = Hub(access=a)
    try:
        _joined(h, a)
        ctx = _ctx(h, a)
        ctx.body = {"secret_id": "11" * 16, "day": "2026-10-29", "n": 1}
        ctx.now_ms = G.NOW
        ctx.registry = h.registry
        org_cleanup(a).mark(G.WS_A)
        with pytest.raises(Refused) as e:
            secret_use(ctx)
        assert e.value.code == "unavailable"
    finally:
        h.close()
        a.close()


def test_delta2_a_mark_that_cannot_be_written_aborts_the_binding_change(monkeypatch):
    a = Access(":memory:")
    h = Hub(access=a)
    try:
        c = _joined(h, a)
        real = Access._w

        def failing(self, sql, args=()):
            if "org_cleanup_pending" in sql:
                from orch_hub.access.store import AccessError

                raise AccessError("store_error", "disk")
            return real(self, sql, args)

        monkeypatch.setattr(Access, "_w", failing)
        before = h.registry.binding_state(G.WS_A)
        with pytest.raises(Exception) as e:
            c.request("binding_put", {"object": G.unbound(seq=2)})
        assert "unavailable" in str(e.value)
        assert h.registry.binding_state(G.WS_A) == before  # not unbound: aborted before the commit
        assert left(a, G.WS_A)["wraps"]  # and nothing was dropped
        monkeypatch.undo()
        assert not org_cleanup(a).is_pending(G.WS_A)  # a refused mark leaves nothing owed
    finally:
        h.close()
        a.close()
