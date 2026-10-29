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
from ..publish import AdminReadClient, AdminReadError
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

    def _h_a_published(self, c: Any, p: dict, q: dict, b: dict | None):
        reg = self.d.registry
        full = [ws for ws in sorted(reg.pins()) if reg.mode(ws) == "full"]
        fallback = owner_fallback_names(reg, self.d.access)

        def name(ws: str) -> str:
            n = reg.display_name(ws)
            return render_text(n) if type(n) is str and n else fallback.get(ws, "A workspace")

        hidden = [{"workspace_id": ws, "name": name(ws)} for ws in sorted(reg.pins()) if reg.mode(ws) == "selective"]
        now = int(self.d.now_ms())
        cache = self.d.__dict__.setdefault("_published_cache", {})
        try:
            rows = self._published_reader().published()
            # the hub's own check of scope, after publish's: only rows of workspaces bound in full mode are shown, and
            # every row has exactly the C1 shape (an answer that fails it is not displayed and not kept)
            rows = check_published_rows([r for r in rows if type(r) is dict and r.get("workspace_id") in full], full)
        except AdminReadError, Refused:
            if "answer" not in cache:
                raise _err("unavailable") from None
            return 200, {**cache["answer"], "stale_ms": max(0, now - cache["at"])}
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
        cache["answer"], cache["at"] = answer, now
        return 200, answer
