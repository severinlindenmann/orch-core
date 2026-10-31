"""The Published page's read (M4 #1357, S1 #1452): ``GET /admin/published``. A mixin of :class:`~orch_hub.webapi.api.WebApi`.

The company list of shared links (and, later, apps) comes from orch-publish through the admin read (``AdminReadClient``,
protocol.md §10.6 and §11.5). The hub stores nothing of it: no token, hash, password record, path or content ever reaches
it (the client refuses a row that carries one), and it adds no authority. Rows of a workspace that is not a bound
FULL-mode workspace are dropped here again (condition 13): a selective workspace shares no details with the hub, so it is
listed only by name under ``hidden``. Every text the workspace asserted goes out through ``render_text``.

When orch-publish cannot be read, the last good answer is returned with its age (``stale_ms``); with none, ``unavailable``."""

from __future__ import annotations

from typing import Any

from ..contract.hub import Refused
from ..contract.phase4 import render_text
from ..contract.published import check_published_rows
from ..publish import AdminReadClient, AdminReadError, NarrowClient, NarrowError
from ..switch import owner_fallback_names


def published_routes(r: Any) -> tuple[Any, ...]:
    return (
        r(
            "GET",
            "/admin/published",
            "admin",
            "a_published",
            "Published: the company's shared links and apps (full-mode workspaces; admin read at publish)",
        ),
        r(
            "POST",
            "/admin/published/links/{share_id}/revoke",
            "admin",
            "a_published_revoke",
            "Revoke a shared link of a full-mode workspace (narrowing only; signed with HK_op, sent to publish)",
        ),
        r(
            "POST",
            "/admin/published/apps/{app_id}/stop",
            "admin",
            "a_published_stop",
            "Stop an app of a full-mode workspace (narrowing only; signed with HK_op, sent to publish)",
        ),
    )


def _err(code: str) -> Exception:
    from .api import ApiError  # late: api.py imports this module through features.py

    return ApiError(code)


def _text(x: Any) -> str | None:
    return render_text(x) if type(x) is str else None


class PublishedHandlers:
    d: Any

    def _published_reader(self) -> Any:
        """Anything with ``published(workspace=None) -> rows``: injected (tests), else built from the publish address."""
        injected = getattr(self.d, "published", None)
        if injected is not None:
            return injected
        url = getattr(self.d.services, "publish_url", None) if self.d.services is not None else None
        if not url:
            raise _err("unavailable")
        try:
            return AdminReadClient(url, self.d.keys.op_signer, self.d.keys.hub_id)
        except ValueError, OSError:
            raise _err("unavailable") from None

    def _narrower(self) -> Any:
        """Anything with ``narrow(workspace_id, target, id) -> result``: injected (tests), else built from the publish
        address the hub is configured with. The browser never supplies an address."""
        injected = getattr(self.d, "narrower", None)
        if injected is not None:
            return injected
        url = getattr(self.d.services, "publish_url", None) if self.d.services is not None else None
        if not url:
            raise _err("unavailable")
        try:
            return NarrowClient(url, self.d.keys.op_signer, self.d.keys.hub_id)
        except ValueError, OSError:
            raise _err("unavailable") from None

    def _narrow(self, c: Any, item_id: Any, kind: str, target: str, activity: str, field: str):
        """Revoke a link / stop an app. The row is found in publish's own admin read (never from the browser), and its
        workspace must be a bound FULL-mode workspace of this hub right now. Nothing here can widen anything."""
        if type(item_id) is not str or len(item_id) != 32 or any(ch not in "0123456789abcdef" for ch in item_id):
            raise _err("unknown_item")
        reg = self.d.registry
        try:
            raw = self._published_reader().published()
        except AdminReadError:
            raise _err("unavailable") from None
        full = [ws for ws in sorted(reg.pins()) if reg.mode(ws) == "full"]
        try:
            rows = check_published_rows([r for r in raw if type(r) is dict and r.get("workspace_id") in full], full)
        except Refused:
            raise _err("unavailable") from None
        row = next((r for r in rows if r["id"] == item_id and r["type"] == kind), None)
        if row is None:
            raise _err("unknown_item")
        try:
            result = self._narrower().narrow(row["workspace_id"], target, item_id)
        except NarrowError as e:
            raise _err(e.code) from None
        self.d.__dict__.pop("_published_cache", None)  # the next list is read again
        if result != "unchanged":
            self._record(
                activity,
                actor_id=c.user_id,
                workspace_id=row["workspace_id"],
                detail={field: item_id},
            )
        return 200, {"id": item_id, "state": "revoked" if result == "revoked" else "stopped", "result": result}

    def _h_a_published_revoke(self, c: Any, p: dict, q: dict, b: dict | None):
        return self._narrow(c, p.get("share_id"), "link", "share", "link.revoked", "link")

    def _h_a_published_stop(self, c: Any, p: dict, q: dict, b: dict | None):
        return self._narrow(c, p.get("app_id"), "app", "app", "app.stopped", "app")

    def _h_a_published(self, c: Any, p: dict, q: dict, b: dict | None):
        reg = self.d.registry
        now = int(self.d.now_ms())
        failed, raw = False, []
        try:
            raw = self._published_reader().published()
        except AdminReadError:
            failed = True
        # the registry is read AFTER the (slow) publish call, so a workspace unbound or switched meanwhile is out
        full = [ws for ws in sorted(reg.pins()) if reg.mode(ws) == "full"]
        fallback = owner_fallback_names(reg, self.d.access)

        def name(ws: str) -> str:
            n = reg.display_name(ws)
            return render_text(n) if type(n) is str and n else fallback.get(ws, "A workspace")

        people = {u["user_id"]: render_text(u["name"]) for u in self.d.access.users() if type(u.get("name")) is str}
        # owner_name: who to ask (the registry's owner alias, a known person) or None; rendered text, never an id
        hidden = [
            {"workspace_id": ws, "name": name(ws), "owner_name": people.get(reg.owner_alias(ws))}
            for ws in sorted(reg.pins())
            if reg.mode(ws) == "selective"
        ]

        def seq(ws: str) -> Any:
            st = reg.binding_state(ws)
            return st.get("binding_seq") if st else None

        cache = self.d.__dict__.get("_published_cache", {})
        rows: list[dict[str, Any]] = []
        if not failed:
            try:
                # the hub's own check of scope, after publish's: only rows of workspaces bound in full mode are shown,
                # and every row has exactly the C1 shape (an answer that fails it is not displayed and not kept)
                rows = check_published_rows([r for r in raw if type(r) is dict and r.get("workspace_id") in full], full)
            except Refused:
                failed = True
        if failed:
            if "answer" not in cache:
                raise _err("unavailable") from None
            # the old answer is filtered by the CURRENT registry again: a workspace that has since left, been unbound or
            # changed its binding (seq or mode) is dropped, and the hidden list is the current one
            keep = {ws for ws in full if cache["seq"].get(ws, False) == seq(ws)}
            old = cache["answer"]
            return 200, {
                **old,
                "links": [x for x in old["links"] if x["workspace_id"] in keep],
                "apps": [x for x in old["apps"] if x["workspace_id"] in keep],
                "hidden": hidden,
                "stale_ms": max(0, now - cache["at"]),
            }
        links, apps = [], []
        for r in rows:
            out = {
                "id": r["id"],
                "workspace_id": r["workspace_id"],
                "workspace": name(r["workspace_id"]),
                "kind": r["kind"],
                "state": r["state"],
                "created_ms": r["created_ms"],
                "bytes": r["bytes"],
                "label": render_text(r["label"]),
                "ticket": _text(r["ticket"]),
                "by_kind": r["by_kind"],
                "by_name": _text(r["by_name"]),
                "has_password": r["has_password"],
            }
            if r["type"] == "link":
                links.append({**out, "expires_ms": r["expires_ms"], "views": r["views"]})
            else:
                apps.append(
                    {
                        **out,
                        "updated_ms": r["updated_ms"],
                        "slug": r["slug"],
                        "app_name": render_text(r["name"]),
                        "access": r["access"],
                        "version": r["version"],
                        "visits_30d": r["visits_30d"],
                    }
                )
        links.sort(key=lambda x: (-x["created_ms"], x["id"]))
        apps.sort(key=lambda x: (-x["created_ms"], x["id"]))
        answer = {"links": links, "apps": apps, "hidden": hidden, "stale_ms": None}
        self.d.__dict__["_published_cache"] = {"answer": answer, "at": now, "seq": {ws: seq(ws) for ws in full}}
        return 200, answer
