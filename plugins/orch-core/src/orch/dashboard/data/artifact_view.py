"""The ticket page's Artifacts area: what the ticket links, grouped by kind, each with the task or criterion it
proves, plus the files in its folder that it does not link yet (orch.core.artifacts). Labels and URLs are agent
text: the template escapes them, and a URL is a link only when it is http(s) (it is never fetched)."""
from __future__ import annotations

from orch.core import artifacts as art
from orch.dashboard.markdown import artifact_scope, artifact_url, inline_size_ok

TITLES = {"screenshot": "Screenshots", "diagram": "Diagrams", "report": "Reports", "dataset": "Datasets",
          "log": "Logs", "build": "Builds", "link": "Links", "feedback": "Your feedback", "other": "Other files"}
ORDER = ("screenshot", "diagram", "report", "dataset", "log", "build", "link", "feedback", "other")


def widget_files(ws, t) -> list[dict]:
    """The files the ticket's widget blocks pin (section, ref, state ok/changed/missing). Linked only while the bytes
    still match the pin, through the artifact route's read-once `?v=` path; never a filesystem path, and only for a
    file of this ticket's own folder (orch.widgets.artifacts.resolve), so a changed or foreign file has no link."""
    from urllib.parse import quote
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


def view(ws, t) -> dict:
    scope = artifact_scope(t, ws)
    changed = dict(art.changed(ws, t))
    items = []
    for e in art.entries(t):
        src = art.source(e)
        item = {"kind": art.kind_of(e), "source": src, "label": art.label_of(e), "task": e.get("task"),
                "ac": e.get("ac") if isinstance(e.get("ac"), int) and not isinstance(e.get("ac"), bool) else None,
                "href": None, "image": False, "text": "", "state": None}
        if src == "name":
            item["text"] = e["name"]
            item["state"] = changed.get(e["name"])
            if item["state"] is None:
                item["href"] = artifact_url(scope, e["name"], e)
                item["image"] = (art.is_image(e["name"]) and bool(e.get("sha256"))
                                 and inline_size_ok(scope, e["name"]))
        elif src == "url":
            item["text"] = e["url"]
            item["href"] = e["url"]  # entries() keeps http(s) URLs only
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
    # M: the aside panel shows images as a grid, files as rows and web links apart (each group keeps its kind order)
    panel = [{"key": "images", "title": "Screenshots and diagrams", "items": [i for i in items if i["image"]]},
             {"key": "files", "title": "Reports and files", "items": [i for i in items if not i["image"] and i["source"] != "url"]},
             {"key": "links", "title": "Links", "items": [i for i in items if i["source"] == "url"]}]
    if wfiles:
        panel.append({"key": "widgets", "title": "Widget files", "items": wfiles})
    return {"count": len(items) + len({w["name"] for w in wfiles} - {i["text"] for i in items if i["source"] == "name"}), "groups": [g for g in groups if g["items"]], "by_ac": by_ac, "by_task": by_task,
            "panel": [g for g in panel if g["items"]],
            "unlinked": art.unregistered(ws, t) + art.unregistered_static(ws, t)}
