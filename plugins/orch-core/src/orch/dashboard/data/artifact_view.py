"""The ticket page's Artifacts area: what the ticket links, grouped by kind, each with the task or criterion it
proves, plus the files in its folder that it does not link yet (orch.core.artifacts). Labels and URLs are agent
text: the template escapes them, and a URL is a link only when it is http(s) (it is never fetched)."""
from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from orch.core import artifacts as art
from orch.dashboard.markdown import artifact_scope, artifact_url, inline_size_ok

TITLES = {"screenshot": "Screenshots", "diagram": "Diagrams", "report": "Reports", "dataset": "Datasets",
          "log": "Logs", "build": "Builds", "link": "Links", "feedback": "Your feedback", "other": "Other files"}
ORDER = ("screenshot", "diagram", "report", "dataset", "log", "build", "link", "feedback", "other")
FOLD_AFTER = 5  # a group with more rows than this shows the first FOLD_AFTER - 1 and folds the rest

# The file type a row's icon shows (from the extension, never from agent text) and how a click opens it: "md"
# rendered, "text" as numbered lines, "table" as a table, "image" in the lightbox (these four through the ticket's
# viewer route, from the pinned bytes), "tab" in a new tab (the sandboxed artifact route), "link" a web link.
_TYPES = (
    ({".md", ".markdown"}, "md", "MD", "md"),
    ({".html", ".htm", ".xhtml"}, "html", "HTML", "tab"),
    ({".json", ".jsonl", ".yaml", ".yml", ".toml"}, "json", None, "text"),
    ({".csv", ".tsv"}, "csv", None, "table"),
    ({".py", ".sh", ".js", ".ts", ".sql", ".rb", ".go", ".rs", ".java", ".kt", ".ps1", ".r", ".scala"}, "code", None,
     "text"),
    ({".txt", ".log", ".out", ".diff", ".patch", ".xml", ".ini", ".cfg"}, "txt", None, "text"),
    ({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}, "img", "IMG", "image"),
    ({".pdf"}, "pdf", "PDF", "tab"),
)


def file_type(name: str | None, source: str) -> dict:
    """{icon, badge, viewer} for an artifact: `icon` is a CSS modifier, `badge` the up to four letters in it."""
    if source == "url":
        return {"icon": "link", "badge": "", "viewer": "link"}
    ext = PurePosixPath(str(name or "")).suffix.lower()
    for exts, icon, badge, viewer in _TYPES:
        if ext in exts:
            return {"icon": icon, "badge": badge or ext[1:5].upper(), "viewer": viewer}
    return {"icon": "file", "badge": ext[1:5].upper() or "FILE", "viewer": "tab"}


def human_size(n: int | None) -> str:
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB".replace(".0 KB", " KB")
    return f"{n / 1024 / 1024:.1f} MB"


def _domain(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def _disk_size(scope, name: str) -> int | None:
    """The size on disk (the frontmatter size is agent-written)."""
    if scope is None or scope.base is None:
        return None
    try:
        return (scope.base / name).stat().st_size
    except OSError:
        return None


def _group(key: str, title: str, items: list) -> dict:
    rows = [i for i in items if not i["image"]]
    shown, more = (rows, []) if len(rows) <= FOLD_AFTER else (rows[:FOLD_AFTER - 1], rows[FOLD_AFTER - 1:])
    return {"key": key, "title": title, "count": len(items), "images": [i for i in items if i["image"]],
            "rows": shown, "more": more}


def files_panel(items: list, mode: str) -> list[dict]:
    """The Files panel's groups: by criterion ("AC2" ..., then the files tied to none) or by orch's own kinds. Each
    group has its images (a thumbnail grid) and its other rows, the rows past FOLD_AFTER folded."""
    if mode == "ac":
        acs = sorted({i["ac"] for i in items if i["ac"] is not None})
        groups = [_group(f"ac{n}", f"AC{n}", [i for i in items if i["ac"] == n]) for n in acs]
        rest = [i for i in items if i["ac"] is None]
        if rest:
            groups.append(_group("none", "Not tied to a criterion", rest))
        return groups
    return [_group(k, TITLES[k], [i for i in items if i["kind"] == k]) for k in ORDER
            if any(i["kind"] == k for i in items)]


def widget_files(ws, t) -> list[dict]:
    """The files the ticket's widget blocks pin (section, ref, state ok/changed/missing). Linked only while the bytes
    still match the pin, through the artifact route's read-once `?v=` path; never a filesystem path, and only for a
    file of this ticket's own folder (orch.widgets.artifacts.resolve), so a changed or foreign file has no link."""
    from orch.core.artifacts import safe_name
    from orch.widgets import artifacts as wa
    from orch.widgets.blocks import has_blocks, ticket_blocks
    if not has_blocks("\n".join(t.sections.values())):
        return []
    out, seen = [], set()
    for b in ticket_blocks(t):
        if not isinstance(b.data, dict) or b.layer is None:
            continue
        for r in wa.refs(b.data):
            ref, digest = r["ref"], r["sha256"]
            if (b.section, ref, digest) in seen:
                continue
            seen.add((b.section, ref, digest))
            name = wa.name_of(t.id, ref)
            state = wa.state(ws, t.id, ref, digest)
            ok = state == "ok" and name and safe_name(name) and isinstance(digest, str) and len(digest) == 64
            out.append({"name": name or ref, "ref": ref, "section": b.section, "state": state,
                        "href": f"/a/{quote(t.id)}/{quote(name)}?v={digest[:16]}" if ok else None})
    return out


def view(ws, t, mode: str = "") -> dict:
    """What the ticket links. `mode` groups the Files panel ("ac" or "type"); empty: by criterion while the ticket is
    in testing and a file proves a criterion, else by type."""
    scope = artifact_scope(t, ws)
    changed = dict(art.changed(ws, t))
    items = []
    for e in art.entries(t):
        src = art.source(e)
        item = {"kind": art.kind_of(e), "source": src, "label": art.label_of(e), "task": e.get("task"),
                "ac": e.get("ac") if isinstance(e.get("ac"), int) and not isinstance(e.get("ac"), bool) else None,
                "href": None, "image": False, "text": "", "state": None, "view": None, "size": "", "domain": "",
                **file_type(e.get("name") or e.get("static"), src)}
        if src == "name":
            item["text"] = e["name"]
            item["state"] = changed.get(e["name"])
            item["size"] = human_size(_disk_size(scope, e["name"]))
            if item["state"] is None:
                item["href"] = artifact_url(scope, e["name"], e)
                item["image"] = (art.is_image(e["name"]) and bool(e.get("sha256"))
                                 and inline_size_ok(scope, e["name"]))
                if item["viewer"] in ("md", "text", "table", "image") and e.get("sha256"):
                    # the viewer reads the same pinned bytes the artifact route serves (?v=)
                    item["view"] = f"/t/{quote(t.id)}/view/{quote(e['name'])}?v={str(e['sha256'])[:16]}"
        elif src == "url":
            item["text"] = e["url"]
            item["href"] = e["url"]  # entries() keeps http(s) URLs only
            item["domain"] = _domain(e["url"])
        else:
            item["text"] = f"orchestrator/static/{e['static']}"
        items.append(item)
    wfiles = widget_files(ws, t)
    groups = [{"kind": k, "title": TITLES[k], "items": [i for i in items if i["kind"] == k]} for k in ORDER]
    by_ac: dict[int, list] = {}
    by_task: dict[str, list] = {}
    for i in items:
        if i["ac"] is not None:
            by_ac.setdefault(i["ac"], []).append(i)
        if isinstance(i["task"], str) and i["task"]:
            by_task.setdefault(i["task"], []).append(i)
    if mode not in ("ac", "type"):
        mode = "ac" if t.status == "testing" and by_ac else "type"
    return {"count": len(items) + len({w["name"] for w in wfiles} - {i["text"] for i in items if i["source"] == "name"}),
            "groups": [g for g in groups if g["items"]], "by_ac": by_ac, "by_task": by_task,
            "mode": mode, "files": files_panel(items, mode), "widget_files": wfiles, "has_ac": bool(by_ac),
            "viewable": [i for i in items if i["view"]],
            "unlinked": art.unregistered(ws, t) + art.unregistered_static(ws, t)}
