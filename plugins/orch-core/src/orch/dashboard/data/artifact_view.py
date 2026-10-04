"""The ticket page's Artifacts area: what the ticket links, grouped by kind, each with the task or criterion it
proves, plus the files in its folder that it does not link yet (orch.core.artifacts). Labels and URLs are agent
text: the template escapes them, and a URL is a link only when it is http(s) (it is never fetched)."""
from __future__ import annotations

from orch.core import artifacts as art
from orch.dashboard.markdown import artifact_scope, artifact_url, inline_size_ok

TITLES = {"screenshot": "Screenshots", "diagram": "Diagrams", "report": "Reports", "dataset": "Datasets",
          "log": "Logs", "build": "Builds", "link": "Links", "other": "Other files"}
ORDER = ("screenshot", "diagram", "report", "dataset", "log", "build", "link", "other")


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
    return {"count": len(items), "groups": [g for g in groups if g["items"]], "by_ac": by_ac, "by_task": by_task,
            "panel": [g for g in panel if g["items"]],
            "unlinked": art.unregistered(ws, t) + art.unregistered_static(ws, t)}
