"""Helpers of the company-secrets tests (M3 S-2): a web API whose registry holds real VERIFIED v2 cards, and the browser's
sealing done with the contract's reference function. The secrets routes are the real ones from ``webapi/api.py``."""

from __future__ import annotations

from webapi_helpers import ORIGIN, Web

from orch_hub.contract import vectorgen as G
from orch_hub.contract.canon import b64u, unb64u
from orch_hub.contract.org_secrets import seal_org_secret
from orch_hub.link import Registry

WS_A, WS_B, WS_C = G.WS_A, G.WS_B, G.WS_C
KEYS = {WS_A: ("wsk_a", "wxk_a"), WS_B: ("wsk_b", "wxk_b"), WS_C: ("wsk_a", "wxk_a")}
VALUE = "sk-live-0123456789abcdef"
DAY = 86_400_000


def card(ws: str, wxk_version: int = 1) -> dict:
    wsk, wxk = KEYS[ws]
    return G.v2_card(ws=ws, wsk=wsk, wxk_pub=G.K.b(wxk), wxk_version=wxk_version)


def join(reg: Registry, ws: str, wxk_version: int = 1, mode: str = "full") -> None:
    wsk, _ = KEYS[ws]
    reg.commit_join(
        ws,
        G.P_ALICE,
        G.K.b(wsk),
        G.b64u(G.K.pub["pk_alice"]),
        card(ws, wxk_version),
        {"status": "bound", "binding": {"mode": mode}},
    )


class SWeb(Web):
    """The Web fixture with a REAL registry (verified cards) and the secrets routes."""

    def __init__(self, tmp_path, monkeypatch, workspaces=(WS_A, WS_B)) -> None:
        super().__init__(tmp_path)
        self.registry = Registry()
        self.api.d.registry = self.registry
        for ws in workspaces:
            join(self.registry, ws)
        self.token = self.login(self.admin)
        self.api.d.now_ms = lambda: G.NOW  # the verified cards are fixed in time; sessions use the Web clock
        self.now = G.NOW

    def close(self) -> None:
        self.registry.close()
        super().close()

    def recipients(self, name="crm-key", shared="all", workspaces=(), expect=200):
        s, o, _ = self.call(
            "POST",
            "/admin/secrets/recipients",
            {"name": name, "shared": shared, "workspaces": list(workspaces)},
            token=self.token,
        )
        assert s == expect, o
        return o

    def seal(self, rec: dict, value: str = VALUE, expires_ms: int | None = None, name="crm-key", only=None) -> dict:
        exp = expires_ms if expires_ms is not None else self.now + 90 * DAY
        wraps = []
        for r in rec["recipients"]:
            if only is not None and r["workspace_id"] not in only:
                continue
            sealed = seal_org_secret(
                unb64u(r["wxk_pub"], 65),
                rec["hub_id"],
                r["workspace_id"],
                rec["secret_id"],
                rec["version"],
                r["wxk_version"],
                exp,
                name,
                value,
            )
            wraps.append({"workspace_id": r["workspace_id"], "wxk_version": r["wxk_version"], "sealed": b64u(sealed)})
        return {"secret_id": rec["secret_id"], "version": rec["version"], "expires_ms": exp, "wraps": wraps}

    def save(self, rec, shared="all", workspaces=(), name="crm-key", expect=200, **kw):
        body = {**self.seal(rec, name=name), "name": name, "shared": shared, "workspaces": list(workspaces)}
        body.update(kw)
        s, o, _ = self.call("POST", "/admin/secrets", body, token=self.token)
        assert s == expect, o
        return o


__all__ = ["DAY", "ORIGIN", "VALUE", "WS_A", "WS_B", "WS_C", "SWeb", "join"]
