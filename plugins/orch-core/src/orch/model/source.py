"""The source list (D58, D59; ticket-format §5.7): the projection of the latest ``branch.pushed`` per linked repo."""

from __future__ import annotations

from typing import Any

from . import generations
from .codes import Code, Refusal
from .types import TCore, WsCore


def linked_repos(t: TCore) -> list[str]:
    return list(t.fields["links"]["repos"])


def missing_repos(t: TCore) -> list[str]:
    """Linked repos never observed by the host (no ``branch.pushed`` yet)."""
    return [r for r in linked_repos(t) if r not in t.branch_heads]


def source_list(t: TCore) -> list[dict[str, str]]:
    """One ``{repo, ref, sha}`` per observed linked repo, sorted by repo identity (§5.7)."""
    out = [
        {"repo": h["repo_id"], "ref": h["ref"], "sha": h["sha"]}
        for r in linked_repos(t)
        if (h := t.branch_heads.get(r)) is not None
    ]
    return sorted(out, key=lambda x: x["repo"])


def repo_sha(t: TCore, repo_name: str | None) -> str | None:
    h = t.branch_heads.get(repo_name) if repo_name else None
    return h["sha"] if h else None


def branch_pushed(ws: WsCore, t: TCore, e: dict[str, Any]) -> Refusal | None:
    """Host event: sets one source-list entry; raises ``verify`` and ``code``; a ``done`` ticket goes back to
    ``testing`` (§5.12)."""
    name = e["repo_name"]
    if name not in linked_repos(t):
        return Refusal(Code.SOURCE_UNLINKED, f"{name} is not in links.repos")
    cur = t.branch_heads.get(name)
    new = {"repo_id": e["repo_id"], "ref": e["ref"], "sha": e["sha"]}
    if e["before"] != cur:
        return Refusal(Code.SOURCE_NOT_NEW, "`before` is not the current source-list entry")
    if new == cur:
        return Refusal(Code.SOURCE_NOT_NEW, "nothing changed")
    if t.status == "done" and (cur is None or (cur["repo_id"], cur["ref"]) != (new["repo_id"], new["ref"])):
        return Refusal(Code.SOURCE_NOT_NEW, "on a done ticket only a new sha on an existing ref counts")
    t.branch_heads[name] = new
    generations.mark(t, "verify", "code")
    if t.status == "done":
        t.status = "testing"
    return None
