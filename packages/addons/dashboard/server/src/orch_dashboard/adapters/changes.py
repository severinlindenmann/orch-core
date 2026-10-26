"""`diff` result as the UI's TicketChanges: one entry per file, its hunks as the diff widget draws them."""

import re

SECTION = re.compile(r"^diff --git a/(.*?) b/(.*)$", re.M)


def _sections(unified: str) -> list[tuple[str, str]]:
    """(new path, hunk lines) for every file section of a unified diff, in order. Headers are dropped."""
    marks = list(SECTION.finditer(unified))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(unified)
        body = unified[m.end() : end].lstrip("\n")
        at = body.find("@@")
        out.append((m.group(2), body[at:].rstrip("\n") if at >= 0 else "(binary or empty change)"))
    return out


def changes(data: dict, key: str) -> dict:
    sections = _sections(data["unified"])
    used: set[int] = set()
    files = []
    for f in data["files"]:
        at = next((i for i, (path, _) in enumerate(sections) if i not in used and path == f["path"]), None)
        if at is not None:
            used.add(at)
        files.append(
            {
                "path": f["path"],
                "repo": f.get("repo"),
                "old_path": f.get("old_path"),
                "status": f["status"],
                "binary": f["binary"],
                "additions": f["add"],
                "deletions": f["del"],
                # A file cut off by max_bytes has no section: say so instead of showing an empty diff.
                "lines": sections[at][1] if at is not None else "(cut off: the diff is truncated)",
            }
        )
    first = data["sources"][0] if data["sources"] else None
    return {
        "ticket": key,
        "branch": first["branch"] if first else "",
        "base": first["base_sha"] if first else "",
        "head": first["head_sha"] if first else "",
        "sources": [
            {"repo": s["repo"], "branch": s["branch"], "base": s["base_sha"], "head": s["head_sha"]}
            for s in data["sources"]
        ],
        "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files),
        "truncated": data["truncated"],
        "files": files,
    }
