"""Import, Close local, Reopen local and Ignore (spec v2 §13.2): declared in the manifest and run only from orch's
human POST. act() never gets an Ops: import, close and reopen (declared with "tickets": true) return an Intent whose
ref is the clicked target, and orch runs it as the human. Each action re-checks the current cache and the core's link
index first, so a stale or forged target changes nothing. Titles come from the cache, never from the request."""
from __future__ import annotations

import re

from orch.addons.api import MAX_ASK_TEXT, Intent
from orch.errors import ValidationError

from .gh import GhFailure, run_json
from .ignore import add_ignored, token

_TICKET = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
_ISSUE_URL = re.compile(r"https://github\.com/(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/issues/(?P<number>\d+)")
_CATEGORIES = ("todo", "in_progress", "done")


def _items(ctx) -> list[dict]:
    return [i for snap in ctx.snapshots("issues") for i in snap.items if isinstance(i, dict) and isinstance(i.get("key"), str)]


def _item(ctx, key: str) -> dict:
    wanted = str(key).strip().upper()
    for item in _items(ctx):
        if item["key"].upper() == wanted:
            return item
    raise ValidationError(f"{key} is not in the cached GitHub issues; refresh the page and try again")


def _ticket_id(target) -> str:
    tid = str(target).strip()
    if not _TICKET.fullmatch(tid):
        raise ValidationError(f"not a local ticket id: {target!r}")
    return tid.upper()


def _import(target, ctx) -> Intent:
    item = _item(ctx, target)
    linked = ctx.links().for_external(item["key"])
    if linked:
        return Intent("none", reason=f"{item['key']} is already {linked[0].id}")
    title = " ".join(str(item.get("title") or "").split()) or item["key"]
    url = item.get("url") if isinstance(item.get("url"), str) else ""
    ask = _issue_text(ctx, url)
    data = {"ask": ask} if ask else {}
    for field in ("type", "priority"):  # the issue's type:/priority: labels; core ignores values orch does not know
        if isinstance(item.get(field), str) and item[field]:
            data[field] = item[field]
    return Intent("import", ref=item["key"], value=title[:200], reason=f"Imported from {url}" if url else "", data=data)


def _issue_text(ctx, url: str) -> str:
    """The issue's own text for the new ticket's Ask (the human's words, ticket design review §3.1); "" when gh
    cannot get it, so the import still works. Fetched at the human's click, not cached with every issue."""
    m = _ISSUE_URL.fullmatch(url)
    if m is None:
        return ""
    try:
        data = run_json(ctx, ["gh", "issue", "view", m.group("number"), "--repo", m.group("repo"), "--json", "body"],
                        timeout=20)
    except GhFailure:
        return ""
    body = data.get("body") if isinstance(data, dict) else None
    return body.strip()[:MAX_ASK_TEXT] if isinstance(body, str) else ""


def _sync(action_id, target, ctx) -> Intent:
    tid = _ticket_id(target)
    want = "close" if action_id == "close_local" else "reopen"
    links = ctx.links()
    for item in _items(ctx):
        for t, action in links.sync(item["key"], item.get("category")):
            if t.id.upper() == tid and action == want:
                reason = f"{item['key']} is closed in GitHub" if want == "close" else f"{item['key']} is open again in GitHub"
                return Intent(want, ref=t.id, reason=reason)
    raise ValidationError(f"{tid} is no longer out of sync this way; nothing changed")


def _ignore(target, ctx) -> Intent:
    parts = [p.strip() for p in str(target).split("|")]
    if len(parts) != 3 or not all(parts) or parts[2] not in _CATEGORIES:
        raise ValidationError(f"not an out-of-sync target: {target!r}")
    tid, key = _ticket_id(parts[0]), parts[1].upper()
    item = _item(ctx, key)
    hits = ctx.links().sync(item["key"], item.get("category"))
    hit = next((h for h in hits if h[0].id.upper() == tid), None)
    if not hit or item.get("category") != parts[2]:
        raise ValidationError(f"{tid} and {key} are no longer out of sync this way; nothing changed")
    add_ignored(ctx.addon.state_dir, token(hit[0].id, item["key"], item.get("category")))
    return Intent("none", reason=f"Ignored the difference on {hit[0].id} until {item['key']} changes again")


def act(action_id, target, ctx) -> Intent:
    if action_id == "import":
        return _import(target, ctx)
    if action_id in ("close_local", "reopen_local"):
        return _sync(action_id, target, ctx)
    if action_id == "ignore":
        return _ignore(target, ctx)
    raise ValidationError(f"unknown action {action_id!r}")
