"""The workspace as a graph: tickets, the files their commits changed, and the links between tickets.

Built from the same sources as `orch related` (git history, where the commit-msg hook names a ticket in every commit,
and ticket frontmatter); nothing is stored. The dashboard's Graph page and `orch graph --json` read it.

nodes: {id, kind: "ticket", title, status, type, epic, summary, resolution} and {id, kind: "file"} (the file id is the
       repo-qualified path `related.commits` uses)
edges: {source, target, kind, weight?} with kind
       changed   ticket -> file, weight = commits of the ticket that changed the file
       parent    epic -> child
       follow_up source ticket -> follow-up
       blocks    blocker -> blocked ticket (from `blocked_by`)
collisions: files that two or more open tickets changed, {file, tickets}
"""
from __future__ import annotations

from collections import Counter, defaultdict

from orch.core import related as rel
from orch.core import store
from orch.core.links import LinkIndex
from orch.core.query import resolution

MAX_FILES = 300  # the most-changed files; a page with more is unreadable, and `orch related` covers the long tail


def _epic(meta: dict, metas: dict) -> str | None:
    """The epic a ticket belongs to: itself, or the first epic up its parent chain (a follow-up's source counts)."""
    if meta.get("type") == "epic":
        return str(meta["id"])
    seen = set()
    parent = meta.get("parent")
    while parent and str(parent).upper() not in seen:
        seen.add(str(parent).upper())
        p = metas.get(str(parent).upper())
        if p is None:
            return None
        if p.get("type") == "epic":
            return str(p["id"])
        parent = p.get("parent")
    return None


def build(ws, *, max_commits: int = rel.MAX_COMMITS, max_files: int = MAX_FILES) -> dict:
    entries = [e for e in store.scan(ws) if isinstance(e.meta, dict)]
    metas = {e.id.upper(): e.meta for e in entries}
    ids = {e.id.upper(): e.id for e in entries}
    commits = rel.commits(ws, LinkIndex(ws, entries), max_commits)

    weight: dict[tuple[str, str], int] = Counter()
    touched: Counter = Counter()
    for c in commits:
        for key in c.keys:
            for f in c.files:
                weight[(key, f)] += 1
        for f in c.files:
            touched[f] += 1
    files = {f for f, _ in touched.most_common(max_files)}

    nodes = [{"id": e.id, "kind": "ticket", "title": e.meta.get("title"), "status": e.status,
              "type": e.meta.get("type"), "epic": _epic(e.meta, metas), "summary": e.summary,
              "resolution": resolution(e.meta, e.status)} for e in entries]
    nodes += [{"id": f, "kind": "file"} for f in sorted(files)]

    edges = [{"source": t, "target": f, "kind": "changed", "weight": n} for (t, f), n in sorted(weight.items()) if f in files]
    for e in entries:
        parent = ids.get(str(e.meta.get("parent") or "").upper())
        if parent:
            kind = "parent" if metas[parent.upper()].get("type") == "epic" else "follow_up"
            edges.append({"source": parent, "target": e.id, "kind": kind})
        for ref in e.meta.get("blocked_by") or []:
            blocker = ids.get(str(ref).upper())
            if blocker and blocker != e.id:
                edges.append({"source": blocker, "target": e.id, "kind": "blocks"})

    status = {e.id: e.status for e in entries}
    open_by_file: dict[str, list[str]] = defaultdict(list)
    for (t, f) in sorted(weight):
        if status.get(t) != "done":
            open_by_file[f].append(t)
    collisions = [{"file": f, "tickets": ts} for f, ts in sorted(open_by_file.items()) if len(ts) > 1]

    return {"nodes": nodes, "edges": edges, "collisions": collisions, "commits": len(commits),
            "files_total": len(touched), "repos": [name or "." for name, _ in rel.repos(ws)]}


def render(data: dict) -> str:
    from orch.textsafe import visible
    tickets = sum(1 for n in data["nodes"] if n["kind"] == "ticket")
    files = sum(1 for n in data["nodes"] if n["kind"] == "file")
    kinds = Counter(e["kind"] for e in data["edges"])
    out = [f"{tickets} tickets, {files} files from {data['commits']} commit(s)"
           + (f" (of {data['files_total']} changed)" if data["files_total"] > files else ""),
           "links: " + ", ".join(f"{kinds[k]} {k}" for k in ("changed", "parent", "follow_up", "blocks"))]
    if not data["repos"]:
        out.append("no git checkout found: only ticket links")
    if data["collisions"]:
        out += ["", "collisions: open tickets changing the same file"]
        out += [f"  {visible(c['file'])}  {', '.join(c['tickets'])}" for c in data["collisions"]]
    return "\n".join(out)
