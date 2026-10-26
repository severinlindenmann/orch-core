"""The File Explorer's HTTP surface (issue #491): GET only, typed queries that forbid extras, one guard.

Nothing here writes. Every route starts with `scope.open_scope` (add-on active, owner, roots) and sends every path
through `guard`. The browser names a root by id (`ws`, `repo:<name>`) and a relative path; it never names an absolute
path, an operation or an argument of git.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .. import reads
from ..errors import HostError, fail
from ..gateway import Gateway
from . import describe, git, guard, links, search
from . import scope as scopes

RootId = Annotated[str, StringConstraints(pattern=r"^(?:ws|repo:[a-z0-9][a-z0-9._-]{0,63})$")]
Path = Annotated[str, StringConstraints(max_length=4096)]
Sha = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Where(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: RootId = "ws"
    path: Path = ""


class FileQuery(Where):
    rev: Sha | None = None


class HistoryQuery(Where):
    skip: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=50, ge=1, le=200)


class CommitQuery(Where):
    sha: Sha


class CompareQuery(Where):
    a: Sha
    b: Sha


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: Text
    root: RootId | None = None
    path: Path = ""


class NamesQuery(SearchQuery):
    pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(path: str) -> tuple[str, ...]:
    try:
        return guard.split(path)
    except guard.GuardError as exc:
        raise fail("invalid.input", exc.reason, exit=5) from None


def _refuse(exc: guard.GuardError):
    """A guard refusal as the host-style error the page already understands."""
    if isinstance(exc, guard.Denied):
        return fail("role.denied", f"{exc.reason}: locked.", exit=3)
    if isinstance(exc, guard.Missing):
        return fail("not_found", exc.reason, exit=2)
    if isinstance(exc, guard.Unusable):
        return fail("not_found", exc.reason, exit=2)
    return fail("invalid.input", exc.reason, exit=5)


def register(app: FastAPI, gateway: Gateway) -> None:
    def open_(ws: str):
        return scopes.open_scope(gateway, ws)

    @app.get("/api/w/{ws}/explorer")
    def info(ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        roots = []
        for root in sc.roots.values():
            try:
                with root.dir(()):
                    pass
            except guard.GuardError:
                continue
            roots.append({"id": root.id, "label": root.label, "kind": root.kind, "repo": root.has_git(())})
        return {"available": True, "roots": roots, "search": search.tools()}

    @app.get("/api/w/{ws}/explorer/tree")
    def tree(query: Annotated[Where, Query()], ws: str = Depends(reads.workspace)):
        root = open_(ws).root(query.root)
        parts = _plain(query.path)
        try:
            rows, more = root.entries(parts)
        except guard.GuardError as exc:
            raise _refuse(exc) from None
        return {"root": root.id, "path": "/".join(parts), "entries": rows, "more": more}

    @app.get("/api/w/{ws}/explorer/file")
    def file(query: Annotated[FileQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        root = sc.root(query.root)
        parts = _plain(query.path)
        if not parts:
            raise fail("invalid.input", "Choose a file.", exit=5)
        name = parts[-1]
        base = {"root": root.id, "path": "/".join(parts), "name": name, "label": guard.label(name)}
        reason = guard.deny(parts)
        if reason:
            return {**base, "kind": "locked", "reason": reason}
        try:
            if query.rev:
                repo = git.locate(root, parts)
                if repo is None:
                    raise fail("not_found", "This file is not in a git repository.", exit=2)
                return {**base, "rev": query.rev, **git.version(repo, name, query.rev)}
            st = root.stat_file(parts)
            if stat.S_ISLNK(st.st_mode):
                return {**base, "kind": "link", "size": st.st_size}
            if stat.S_ISDIR(st.st_mode):
                return {**base, "kind": "dir"}
            if not stat.S_ISREG(st.st_mode):
                return {**base, "kind": "other", "size": st.st_size}
            data = None
            cut = False
            if st.st_size <= max(describe.TEXT_MAX, describe.IMAGE_MAX):
                data, st, cut = root.read(parts, max(describe.TEXT_MAX, describe.IMAGE_MAX))
        except guard.GuardError as exc:
            raise _refuse(exc) from None
        out = describe.describe(name, data, st.st_size, cut)
        if out["kind"] in ("text", "markdown") and len(data or b"") > describe.TEXT_MAX:
            out = describe.describe(name, None, st.st_size)
        repo = None
        try:
            repo = git.locate(root, parts)
        except (guard.GuardError, HostError):
            pass
        return {**base, **out, "modified": _iso(st.st_mtime), "history": repo is not None}

    @app.get("/api/w/{ws}/explorer/names")
    def names(query: Annotated[NamesQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        roots = [sc.root(query.root)] if query.root else list(sc.roots.values())
        folder = _plain(query.path) if query.root else ()
        files, cut = [], False
        for root in roots:
            try:
                got = search.names(root, folder, query.q)
            except guard.GuardError as exc:
                if query.root:
                    raise _refuse(exc) from None
                continue
            files += got["files"]
            cut = cut or got["cut"]
        return {"files": files[:100], "cut": cut}

    @app.get("/api/w/{ws}/explorer/search")
    def content(query: Annotated[SearchQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        roots = [sc.root(query.root)] if query.root else list(sc.roots.values())
        folder = _plain(query.path) if query.root else ()
        try:
            return search.search(roots, folder, query.q)
        except guard.GuardError as exc:
            raise _refuse(exc) from None

    def history_target(sc, query):
        """History, a commit and a comparison are for ONE FILE: a regular file on disk (no link), or a file git
        recorded that is gone now. Never a folder (a folder's patch would span every file below it)."""
        root = sc.root(query.root)
        parts = _plain(query.path)
        if not parts:
            raise fail("invalid.input", "Choose a file.", exit=5)
        try:
            repo = git.locate(root, parts)
            try:
                st = root.stat_file(parts)
            except guard.Missing:
                st = None
        except guard.GuardError as exc:
            raise _refuse(exc) from None
        if st is not None and not stat.S_ISREG(st.st_mode):
            raise fail("not_found", "History is for files. Choose a file.", exit=2)
        if st is None and (repo is None or not git.was_file(repo)):
            raise fail("not_found", "There is no such file.", exit=2)
        return root, parts, repo

    def base_of(repo):
        return lambda sha: git.on_head(repo, sha)

    @app.get("/api/w/{ws}/explorer/history")
    def history(query: Annotated[HistoryQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        _root, _parts, repo = history_target(sc, query)
        if repo is None:
            return {"repo": False, "commits": [], "more": False}
        got = git.log(repo, query.skip, query.limit)
        try:
            tickets = links.chips(sc.link, ws, [c["sha"] for c in got["commits"]], is_base=base_of(repo))
        except Exception:  # noqa: BLE001 - chips are a courtesy: the history still shows
            tickets = {}
        for c in got["commits"]:
            c["tickets"] = tickets.get(c["sha"], [])
        return {"repo": True, **got}

    @app.get("/api/w/{ws}/explorer/commit")
    def commit(query: Annotated[CommitQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        _root, _parts, repo = history_target(sc, query)
        if repo is None:
            raise fail("not_found", "This file is not in a git repository.", exit=2)
        got = git.commit(repo, query.sha)
        try:
            got["tickets"] = links.chips(sc.link, ws, [got["sha"]], is_base=base_of(repo)).get(got["sha"], [])
        except Exception:  # noqa: BLE001
            got["tickets"] = []
        return got

    @app.get("/api/w/{ws}/explorer/compare")
    def compare(query: Annotated[CompareQuery, Query()], ws: str = Depends(reads.workspace)):
        sc = open_(ws)
        _root, _parts, repo = history_target(sc, query)
        if repo is None:
            raise fail("not_found", "This file is not in a git repository.", exit=2)
        return git.compare(repo, query.a, query.b)

    @app.get("/api/w/{ws}/explorer/ticket/{key}")
    def ticket_files(key: reads.Key, ws: str = Depends(reads.workspace)):
        """The "Files changed" box of a ticket: the files of the commits its own events name (pushes and receipts)."""
        sc = open_(ws)
        try:
            commits = links.ticket_commits(sc.link, key)
        except Exception:  # noqa: BLE001
            raise fail("not_found", exit=2) from None
        files: dict[tuple[str, str], dict] = {}
        for c in commits:
            place = sc.repos.get(c["repo"] or "")
            if place is None:
                continue
            root_id, prefix = place
            root = sc.roots.get(root_id)
            if root is None:
                continue
            try:
                repo = git.locate(root, (*prefix, "x"))
            except (guard.GuardError, HostError):
                continue
            if repo is None:
                continue
            try:
                if c.get("baseline") and git.on_head(repo, c["sha"]):
                    continue  # the commit the branch was cut from: not work of this ticket
                changed = git.commit_files(repo, c["sha"], c["before"])
            except Exception:  # noqa: BLE001
                continue
            for status, rel in changed[:200]:
                try:
                    parts = (*prefix, *guard.split(rel))
                    if guard.deny(parts):
                        continue
                except guard.GuardError:
                    continue
                files.setdefault(
                    (root_id, "/".join(parts)),
                    {
                        "root": root_id,
                        "path": "/".join(parts),
                        "label": guard.label("/".join(parts)),
                        "status": status if status in "AMDRTC" else "M",
                        "sha": c["sha"],
                        "short": c["sha"][:8],
                        "via": c["via"],
                        "repo": c["repo"],
                    },
                )
        rows = sorted(files.values(), key=lambda r: (r["path"], r["root"]))
        return {"files": rows[:300], "more": len(rows) > 300}
