"""Widgets of the wiki addon: the Wiki page (search, docs that may need an update, pages linked from open tickets,
recently changed), the ticket.pages panel and Today decisions. Reads cached snapshots and the index file only."""
from __future__ import annotations

import re
from datetime import datetime

from orch.addons.api import PendingDecision
from orch.addons.widgets import Action, Callout, Card, Copy, Link, Markdown, Search, Table, Time, safe_url
from orch.clock import now as clock_now

from .create import page_id
from .github_wiki import wiki_repos
from .local import folder_of, resolve_folder
from .relate import TARGET, related_pages, search

DIFFS = "branch-diffs"
TICKET_KEY = re.compile(r"[A-Z][A-Z0-9]*-\d+")


def _str(value, cap: int = 200) -> str:
    return " ".join(str(value if value is not None else "").split())[:cap]


def ago(value) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return "unknown"
    if dt.tzinfo is None:
        return "unknown"
    secs = max(0, int((clock_now() - dt).total_seconds()))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def selected(view) -> str:
    return view.settings.get("provider") or "github-wiki"


def pages_of(view) -> list[dict]:
    """Pages of the selected provider only: a cache left by another provider is not shown after switching."""
    out = []
    provider = selected(view)
    for snap in view.snapshots():
        if snap.provider == provider:
            out.extend(i for i in snap.items if isinstance(i, dict) and i.get("id") and i.get("space"))
    return out


def diff_items(view) -> list[dict]:
    return [i for snap in view.snapshots(DIFFS) for i in snap.items if isinstance(i, dict)]


def page_link(page: dict):
    title = _str(page.get("title") or page.get("id")) or "page"
    return Link(title, page["url"]) if safe_url(page.get("url")) else title


def _dismissable(hint) -> bool:
    return len(hint.key) <= 500 and TARGET.fullmatch(hint.key) is not None


def _hint_title(hint) -> str:
    return _str(hint.title, 100)


def local_view(view, addon, pages: list[dict], pid: str) -> list:
    """One local page: its stale hints, the path to copy and the Markdown body full width."""
    found = next((p for p in pages if p.get("id") == pid), None)
    if found is None:
        return [Callout("warn", "Page not found", f"There is no page {_str(pid, 100)} in {folder_of(view.settings)}/. "
                        "It may have been renamed or deleted."), Link("All pages", "/addons/wiki/")]
    out = []
    for h in addon.hints(view):
        if h.space == found.get("space") and h.page_id == pid:
            out.append(Callout("info", f"{_hint_title(h)} documents files {h.ticket} changed — may need an update", _str(h.reason, 300)))
            if _dismissable(h):
                out.append(Action("dismiss", "Dismiss", h.key))
    body = addon.body_of(found)
    out.append(Link("All pages", "/addons/wiki/"))
    out.append(Card(_str(found.get("title") or pid), (
        Copy("Copy path", _str(found.get("path"), 500)),
        Markdown(body, here=pid, pages=tuple(sorted(str(p["id"]) for p in pages))[:500], widgets=True,
                 files=_str(found.get("space"), 500)) if body is not None else Callout("warn", "Page text not cached", "Press Refresh to read it again."))))
    return out


def local_pages_card(pages: list[dict]) -> Card:
    rows = tuple((page_link(p), str(p["id"]).rpartition("/")[0] or None, Time(p["updated_at"]) if p.get("updated_at") else None)
                 for p in sorted(pages, key=lambda p: str(p["id"]).lower()))
    return Card("Pages", (Table(("Page", "Folder", "Updated"), rows),))


def page(view, addon) -> list:
    query = _str(getattr(view, "params", {}).get("q", ""), 200)
    pages = pages_of(view)
    local = selected(view) == "local"
    pid = getattr(view, "params", {}).get("page", "") if local else ""
    if pid:
        return local_view(view, addon, pages, pid)
    out = [Search("q", value=query, placeholder="title or text")]
    if local:
        folder, why = resolve_folder(view.root, view.settings)
        rel = folder_of(view.settings)
        if folder is None:
            out.append(Callout("err", "Wiki folder not usable", why))
        elif not pages:
            out.append(Callout("info", f"No pages in {rel}/ yet",
                               "Add Markdown files to this folder and press Refresh, or create one from a ticket."))
            out.append(Copy("Copy folder path", rel))
    elif selected(view) == "confluence":
        out.append(Callout("warn", "Confluence is not available yet", "Set the wiki provider to github-wiki in Workspace & addons."))
    elif not wiki_repos(view.settings, view.root):
        out.append(Callout("info", "No wiki repo set",
                           "Add owner/name in Workspace & addons, or give the harness repo a GitHub origin."))
    if query:
        rows = tuple((page_link(p), ago(p.get("updated_at")), _str(p.get("excerpt"))) for p in search(pages, addon.text_of, query))
        out.append(Card(f"Results for {_str(query, 100)}", (Table(("Page", "Updated", "Excerpt"), rows, empty="No page matches this search. Try other or fewer words."),)))
    hints = addon.hints(view)
    if hints:
        rows = tuple((Link(h.ticket, f"/t/{h.ticket}"), Link(_str(h.title), h.url) if safe_url(h.url) else _str(h.title),
                      _str(h.reason, 300), Action("dismiss", "Dismiss", h.key) if _dismissable(h) else None) for h in hints[:50])
        out.append(Card("Docs that may need an update", (Table(("Ticket", "Page", "Why", ""), rows),)))
    statuses = addon.statuses(view)
    linked = []
    for p in pages:
        keys = p.get("links") if isinstance(p.get("links"), list) else []
        open_keys = [k for k in keys if isinstance(k, str) and k in statuses and statuses[k] != "done"]
        if open_keys:
            linked.append((p, open_keys))
    if linked:
        rows = tuple((page_link(p), ", ".join(keys[:5]), ago(p.get("updated_at"))) for p, keys in linked[:50])
        out.append(Card("Pages linked from open tickets", (Table(("Page", "Tickets", "Updated"), rows),)))
    if local:
        if pages:
            out.append(local_pages_card(pages))
        return out
    recent = sorted(pages, key=lambda p: str(p.get("updated_at") or ""), reverse=True)[:15]
    rows = tuple((page_link(p), ago(p.get("updated_at")), _str(p.get("author")) or "unknown") for p in recent)
    out.append(Card("Recently changed", (Table(("Page", "Updated", "Author"), rows, empty="No pages cached yet. Press Refresh."),)))
    return out


def ticket_panel(view, addon) -> list:
    ticket = view.ticket
    if ticket is None:
        return []
    hints = [h for h in addon.hints(view) if h.ticket == ticket.id][:5]
    pages = pages_of(view)
    related = related_pages(ticket, pages, addon.text_of, addon.mentions_of)
    create = selected(view) == "local" and TICKET_KEY.fullmatch(ticket.id) and not any(
        p.get("id") == page_id(ticket.id) for p in pages)
    if not hints and not related and not create:
        return []
    body = []
    for h in hints:
        body.append(Callout("info", f"{_hint_title(h)} documents files this ticket changed — may need an update", _str(h.reason, 300)))
        if _dismissable(h):
            body.append(Action("dismiss", "Dismiss", h.key))
    if related:
        body.append(Table(("Page", "Why", "Updated"), tuple((page_link(p), why, ago(p.get("updated_at"))) for p, why in related)))
    if create:
        body.append(Action("create_page", "Create page from ticket", ticket.id))
    return [Card("Wiki", tuple(body))]


def decisions(view, addon) -> list:
    return [PendingDecision(id=f"docs|{h.key}",
                            title=f"{_hint_title(h)} documents files {h.ticket} changed — may need an update",
                            body=_str(h.reason, 300), ticket=h.ticket, choices=(("dismiss", "Dismiss"),), role="info")
            for h in addon.hints(view)[:10] if _dismissable(h)]
