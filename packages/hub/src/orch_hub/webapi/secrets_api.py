"""Company secrets admin routes (M3, #1431-#1433). A mixin of :class:`~orch_hub.webapi.api.WebApi` (it only uses ``self.d``
and ``self._record``) plus its route table ``SECRET_ROUTES``.

The hub never sees a value: the admin's browser seals one wrap per recipient workspace (``web/seal.js``) and posts only
the sealed bytes. The request schema has no member for a value and refuses any member it does not know, so a value cannot
ride along by mistake. Recipients and their key versions come ONLY from the verified v2 card of each bound workspace.
Every act writes an activity record that names the secret, never a wrap."""

from __future__ import annotations

import time
from typing import Any

from ..access import AccessError
from ..access.orgsecrets import org_secrets, workspace_wxk
from ..contract import phase4 as P4
from ..contract.canon import CanonError, b64u, unb64u
from ..contract.org_secrets import EXPIRY_MAX_MS, NAME, SECRETS_MAX, VALUE_MAX, VALUE_MIN

#: the only members a save may carry; anything else (a ``value``, ``plaintext``, ``secret_value``...) is ``malformed``
SAVE_FIELDS = frozenset({"name", "secret_id", "version", "expires_ms", "shared", "workspaces", "wraps"})
RECIPIENT_FIELDS = frozenset({"name", "shared", "workspaces"})
SHARE_FIELDS = frozenset({"shared", "workspaces"})
WRAP_FIELDS = frozenset({"workspace_id", "wxk_version", "sealed"})


def _exact(b: dict | None, fields: frozenset[str], optional: frozenset[str] = frozenset()) -> dict:
    if b is None or not fields - optional <= set(b) <= fields:
        raise AccessError("malformed")
    return b


class SecretHandlers:
    d: Any
    _record: Any

    def _now_ms(self) -> int:
        fn = getattr(self.d, "now_ms", None)
        return int(fn()) if callable(fn) else int(time.time() * 1000)

    def _bound(self) -> list[str]:
        reg = self.d.registry
        return sorted(ws for ws in reg.pins() if reg.mode(ws) is not None)

    def _recipients(self, shared: str, workspaces: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """(recipients with a verified key, bound workspaces of the share that have none)."""
        bound = self._bound()
        covered = bound if shared == "all" else [w for w in sorted(set(workspaces)) if w in bound]
        now, ok, missing = self._now_ms(), [], []
        for ws in covered:
            k = workspace_wxk(self.d.registry, ws, now)
            if k is None:
                missing.append(ws)
            else:
                ok.append({"workspace_id": ws, "wxk_pub": b64u(k[0]), "wxk_version": k[1]})
        return ok, missing

    def _h_a_secrets(self, c: Any, p: dict, q: dict, b: dict | None):
        reg, now = self.d.registry, self._now_ms()
        store = org_secrets(self.d.access)
        people = {u["user_id"]: P4.render_text(u["name"]) for u in self.d.access.users()}
        workspaces = []
        for ws in self._bound():
            card = (reg.card(ws) or {}).get("name")
            k = workspace_wxk(reg, ws, now)
            workspaces.append(
                {
                    "workspace_id": ws,
                    "name": card if type(card) is str and card else None,
                    "owner_name": people.get(reg.owner_alias(ws)),  # display name only: never an id or a key
                    "wxk_version": k[1] if k else None,
                }
            )
        keyver = {w["workspace_id"]: w["wxk_version"] for w in workspaces}
        out = []
        for s in store.list(now):
            covered = (
                [w["workspace_id"] for w in workspaces]
                if s["shared"] == "all"
                else [w for w in s["workspaces"] if w in keyver]
            )
            use = store.use_by_workspace(s["id"], now)
            state = {
                w: {
                    "state": "ready"
                    if s["wraps"].get(w) is not None and s["wraps"][w] == keyver.get(w)
                    else "needs_value",
                    "used_this_week": use.get(w, 0),
                }
                for w in covered
            }
            out.append(
                {
                    "name": s["name"],
                    "version": s["version"],
                    "expires_ms": s["expires_ms"],
                    "expired": s["expires_ms"] <= now,
                    "shared": s["shared"],
                    "workspaces": s["workspaces"],
                    "state": state,
                    "used_this_week": s["used_this_week"],  # reported by the hosts: untrusted display data
                }
            )
        return 200, {"secrets": out, "workspaces": workspaces, "max": SECRETS_MAX}

    def _h_a_secret_recipients(self, c: Any, p: dict, q: dict, b: dict | None):
        """What the page needs to seal: the secret id, the next version, the hub id and one ``{workspace_id, wxk_pub,
        wxk_version}`` per bound workspace of the share set that has a verified key. No state changes here."""
        b = _exact(b, RECIPIENT_FIELDS)
        name, shared, ws = b["name"], b["shared"], b["workspaces"]
        if (
            type(name) is not str
            or NAME.fullmatch(name) is None
            or shared not in ("all", "chosen")
            or type(ws) is not list
            or len(ws) > 256
            or any(type(w) is not str for w in ws)
            or (shared == "chosen" and not ws)
        ):
            raise AccessError("malformed")
        store = org_secrets(self.d.access)
        store._admin(c.user_id)
        cur = store.get(name)
        if cur is None:
            import os

            sid, version = os.urandom(16).hex(), 1
        else:
            sid, version = cur["id"], cur["version"] + 1
        ok, missing = self._recipients(shared, ws)
        return 200, {
            "hub_id": self.d.keys.hub_id,
            "secret_id": sid,
            "version": version,
            "recipients": ok,
            "without_key": missing,
            "now_ms": self._now_ms(),
            "max_expiry_ms": EXPIRY_MAX_MS,
            "value_min": VALUE_MIN,
            "value_max": VALUE_MAX,
        }

    def _h_a_secret_save(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _exact(b, SAVE_FIELDS)
        if type(b["workspaces"]) is not list or type(b["wraps"]) is not list or len(b["wraps"]) > 256:
            raise AccessError("malformed")
        wraps = []
        for w in b["wraps"]:
            if type(w) is not dict or set(w) != WRAP_FIELDS or type(w["sealed"]) is not str:
                raise AccessError("malformed")
            try:
                raw = unb64u(w["sealed"])
            except CanonError:
                raise AccessError("malformed") from None
            wraps.append({**w, "sealed": raw})
        shared = b["shared"]
        if shared not in ("all", "chosen") or any(type(w) is not str for w in b["workspaces"]):
            raise AccessError("malformed")
        # the wraps must be exactly the bound workspaces of the share set, each for its CURRENT verified key version
        ok, _missing = self._recipients(shared, b["workspaces"])
        required = {r["workspace_id"]: r["wxk_version"] for r in ok}
        out = org_secrets(self.d.access).put(
            actor=c.user_id,
            name=b["name"],
            secret_id=b["secret_id"],
            version=b["version"],
            expires_ms=b["expires_ms"],
            shared=shared,
            workspaces=b["workspaces"],
            wraps=wraps,
            required=required,
            now_ms=self._now_ms(),
        )
        self._record(
            "secret.created" if out["created"] else "secret.rotated",
            actor_id=c.user_id,
            detail={"secret": b["name"]},
        )
        return 200, {"name": out["name"], "version": out["version"]}

    def _h_a_secret_share(self, c: Any, p: dict, q: dict, b: dict | None):
        b = _exact(b, SHARE_FIELDS)
        out = org_secrets(self.d.access).set_share(p["name"], b["shared"], b["workspaces"], actor=c.user_id)
        self._record("secret.shared", actor_id=c.user_id, detail={"secret": p["name"], "count": len(out["workspaces"])})
        return 200, {"shared": out["shared"], "workspaces": out["workspaces"]}

    def _h_a_secret_delete(self, c: Any, p: dict, q: dict, b: dict | None):
        org_secrets(self.d.access).remove(p["name"], actor=c.user_id, now_ms=self._now_ms())
        self._record("secret.removed", actor_id=c.user_id, detail={"secret": p["name"]})
        return 200, {"ok": True}


def secret_routes(route: Any) -> tuple[Any, ...]:
    """The routes, built with ``api._r`` (passed in, so this module does not import ``api`` and no cycle appears)."""
    return (
        route("GET", "/admin/secrets", "admin", "a_secrets", "Company secrets and which workspaces can read them"),
        route(
            "POST",
            "/admin/secrets/recipients",
            "admin",
            "a_secret_recipients",
            "The workspaces and keys a secret is sealed to (nothing is stored)",
        ),
        route(
            "POST",
            "/admin/secrets",
            "admin",
            "a_secret_save",
            "Save a company secret (sealed wraps only, never a value)",
        ),
        route("PUT", "/admin/secrets/{name}/share", "admin", "a_secret_share", "Change who can read a company secret"),
        route("DELETE", "/admin/secrets/{name}", "admin", "a_secret_delete", "Remove a company secret"),
    )
