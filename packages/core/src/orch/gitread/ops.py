"""The requests the reader answers, the same in the child (host mode) and in-process (direct mode).

* ``pin {path, roots}`` (profile ``pin``): the pins of a working copy (:mod:`orch.gitread.pinning`).
* ``pin_many {paths}`` (profile ``pin``, #471): ``pin`` for each of up to :data:`~orch.gitread.limits.PIN_BATCH`
  distinct paths in one child, answered per path and in order (``{"pins"}`` or ``{"error": {code, message}}``): each
  path is pinned exactly as ``pin`` would (same roots, same checks), and a refusal of one never touches the others.
* ``observe {refs}`` (profile ``gitdir``): ``HEAD``'s branch, the commit each ref names, the raw
  ``remote.origin.url`` values.
* ``diff {branch, default_branch, context, max_bytes}`` (profile ``gitdir``): base and head per section 0.8 and the
  patch text of ``git diff --no-ext-diff --no-textconv --no-renames base head``.
* ``worktree_diff {max_bytes?, max_files?}`` (profile ``worktree`` only): the uncommitted changes of the working copy as
  a unified diff against ``HEAD`` (focus mode P2-5; :func:`worktree_diff`).
* ``tree_check {removal?}`` (profile ``worktree`` only): is the working copy clean relative to ``HEAD``
  (:mod:`orch.gitread.treecheck`; H10, F2, N3).

Every request first opens the repository under its pins and reads all its refs, so a config, extension or ref store
the reader refuses makes **every** request unavailable, not just the ones that would touch the bad part.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from orch import canon  # imported here, before the sandbox hook: the request must not import anything later
from orch.records.tool import _headers_ok, _message_ok  # the one rule for a records commit (not copied)

from . import limits, myers, treecheck  # myers: imported before the sandbox hook, like everything the request uses
from .errors import NotFound, Unavailable
from .fs import RepoPins, open_pin
from .patchtext import Change, diff_trees, quote_path, render
from .pinning import pin_repo
from .repo import Repo
from .treejob import escape_path

__all__ = ["PROFILES", "allowed_paths", "run"]

PROFILES = ("gitdir", "worktree", "pin")
_OPS = {
    "pin": ("pin",),
    "pin_many": ("pin",),
    "observe": ("gitdir", "worktree"),
    "diff": ("gitdir", "worktree"),
    "tree_check": ("worktree",),
    "worktree_diff": ("worktree",),
    "records_identify": ("pin",),
}
_HISTORY_MAX = 20_000  # commits looked at to identify a records repository; more is "cannot tell"
_HEX = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _str(args: dict[str, Any], key: str, *, optional: bool = False) -> str | None:
    v = args.get(key)
    if v is None and optional:
        return None
    if not isinstance(v, str) or not v or len(v) > 4096 or "\0" in v:
        raise Unavailable(f"bad request: {key}")
    return v


def allowed_paths(profile: str, hello: dict[str, Any]) -> list[str]:
    """The directories the profile may open: the pins (``gitdir``: gitdir, ``commondir`` and the working copy's
    ``.git`` alone; ``worktree``: gitdir, ``commondir`` and the working copy) or, for ``pin``, the declared repo
    paths."""
    if profile == "pin":
        roots = hello.get("roots")
        if not isinstance(roots, list) or not roots or not all(isinstance(r, str) and r.startswith("/") for r in roots):
            raise Unavailable("bad request: roots")
        return list(roots)
    pins = RepoPins.from_json(hello.get("pins"))
    out = [pins.gitdir.path, pins.commondir.path]
    if profile == "worktree":
        out.append(pins.worktree.path)
    else:  # #486: exactly the working copy's .git (the Repo open checks it), the working copy only as an ancestor
        out.append(pins.worktree.path.rstrip("/") + "/.git")
    return out


def _branch_ref(name: str) -> str:
    return name if name.startswith("refs/heads/") else "refs/heads/" + name


def observe(repo: Repo, refs: list[str]) -> dict[str, Any]:
    resolved = {r: repo.resolve_commit(r) for r in refs}  # reads the whole ref store first
    try:
        head_ref: str | None = repo.head_ref()
        head_error = None
    except Unavailable as e:
        head_ref, head_error = None, e.reason
    head_sha = repo.resolve_commit(head_ref) if head_ref else None
    return {
        "head_ref": head_ref,
        "head_sha": head_sha,
        "head_error": head_error,
        "refs": resolved,
        "origin_urls": repo.config.origin_urls,
        "object_format": repo.algo,
    }


_BIDI = {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}


def clean_line(raw: bytes, limit: int = 200) -> str:
    """Display text from agent-written bytes: UTF-8 (U+FFFD for bad bytes), with C0/C1 controls, DEL and bidi
    controls removed, cut at ``limit`` characters."""
    text = raw.decode("utf-8", errors="replace")
    out = "".join(ch for ch in text if not (ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 or ord(ch) in _BIDI))
    return out[:limit]


def _too_large(repo: Repo, ch: Change, used: int) -> tuple[str | None, int]:
    """Why a change is too large to diff as text, checked from its blobs **before** the diff code splits anything
    (review finding 2: a 73 KB blob of 16 MiB of newlines cost 2.2 GiB in the line diff). Returns ``(why, lines)``:
    ``why`` is ``None`` when it fits; ``used`` is the lines this request has already diffed."""
    lines = 0
    for side in (ch.old, ch.new):
        if side is None or side[0] & 0o170000 == 0o160000:
            continue
        data = repo.blob(side[1])
        if b"\0" in data[:8000]:
            continue  # binary: never split into lines
        if len(data) > limits.TEXT_BYTES_MAX:
            return f"a side is over {limits.TEXT_BYTES_MAX >> 20} MiB", 0
        n = data.count(b"\n") + 1
        if n > limits.TEXT_LINES_MAX:
            return f"a side has over {limits.TEXT_LINES_MAX} lines", 0
        lines += n
    if used + lines > limits.REQUEST_LINES_MAX:
        return f"this request's budget of {limits.REQUEST_LINES_MAX} diffed lines is spent", 0
    return None, lines


def _marker(ch: Change, why: str) -> bytes:
    a, b = quote_path(b"a/" + ch.path), quote_path(b"b/" + ch.path)
    return b"diff --git " + a + b" " + b + b"\n" + b"Too large to diff: " + why.encode() + b"\n"


_HEXDIGITS = frozenset("0123456789abcdef")
#: qualifying pushed heads that become roots of the authorized ranges, and commits the ranges may hold together (§0.8.5)
ROOTS_MAX = 21
RANGE_COMMITS_MAX = 10_000


def _sha_arg(v: Any, repo: Repo, what: str) -> str:
    if not isinstance(v, str) or len(v) != repo.hexlen or not set(v) <= _HEXDIGITS:
        raise Unavailable(f"bad request: {what}")
    return v


def _entries(v: Any, repo: Repo, what: str, limit: int) -> list[dict[str, Any]]:
    if not isinstance(v, list) or len(v) > limit or not all(isinstance(e, dict) for e in v):
        raise Unavailable(f"bad request: {what}")
    out = []
    for e in v:
        r = e.get("repo")
        if not isinstance(r, str) or not r or len(r) > 512:
            raise Unavailable(f"bad request: {what}")
        out.append({**e, "repo": r, "sha": _sha_arg(e.get("sha"), repo, what)})
    return out


def _same(a: str, b: str) -> bool:
    try:
        return canon.same_repo_identity(a, b)
    except Exception:  # noqa: BLE001 - an identity that does not parse names no repo
        return False


def _default_base(repo: Repo, head: str, tip: str | None, pinned: str | None) -> tuple[str, str, int, str | None]:
    """Section 0.8's base choice for ``head``: ``(base, base_source, base_moved, local merge base)``. The host-pinned
    base while it is an ancestor of ``head`` and of the default branch's tip; else the merge base with that tip; else
    the first parent."""
    mb = repo.merge_base(head, tip) if tip is not None else None
    # a pin counts only while it lies below both the head and the CURRENT default-branch tip: an agent that moved
    # the default branch forward, got a pin recorded and moved it back must not hide its commits behind that pin
    if pinned is not None and tip is not None and pinned in repo.ancestors(head) and pinned in repo.ancestors(tip):
        moved = 0
        if mb is not None and mb != pinned and pinned in repo.ancestors(mb):
            moved = len(repo.range_commits(pinned, mb))
        return pinned, "host_pinned", moved, mb
    if mb is not None:
        return mb, "local_ref", 0, mb
    parents = repo.commit(head).parents
    if not parents:
        raise Unavailable("no merge base with the default branch and no parent commit")
    return parents[0], "first_parent", 0, mb


def _usable(repo: Repo, sha: str) -> bool:
    try:
        repo.commit(sha)
        return True
    except Unavailable:
        return False


def _authorize(
    repo: Repo, ext: dict[str, Any], ident: str, head0: str, tip: str | None, pinned: str | None
) -> tuple[str, str | None, str | None, int]:
    """``head`` and ``base`` of the request (§0.8.5): ``(head, base, base_source, base_moved)`` with ``base`` ``None``
    when the section 0.8 rule picks it. Anything outside the ranges the host derives itself is ``NotFound``."""
    head = ext.get("head")
    base = ext.get("base")
    nope = NotFound("not found for this ticket")
    for v in (head, base):  # a wrong length or alphabet for this repo's object format reads as any other unknown id
        if v is not None and (not isinstance(v, str) or len(v) != repo.hexlen or not set(v) <= _HEXDIGITS):
            raise nope
    pushes = ext.get("pushes") or []
    if not isinstance(pushes, list) or len(pushes) > 128:
        raise Unavailable("bad request: pushes")
    pushed_heads: list[str] = []  # qualifying pushed ``sha``, newest first
    pushed_bases: set[str] = set()  # those and the qualifying ``before.sha``
    for p in pushes:
        if not isinstance(p, dict) or not isinstance(p.get("repo"), str) or not isinstance(p.get("ref"), str):
            raise Unavailable("bad request: pushes")
        if not _same(p["repo"], ident):
            continue  # an old push with another repo identity authorizes nothing
        sha = _sha_arg(p.get("sha"), repo, "pushes")
        pushed_heads.append(sha)
        pushed_bases.add(sha)
        b = p.get("before")
        if (
            isinstance(b, dict)
            and isinstance(b.get("repo"), str)
            and _same(b["repo"], ident)
            and b.get("ref") == p["ref"]
        ):
            pushed_bases.add(_sha_arg(b.get("sha"), repo, "pushes"))
    roots = [head0]
    for sha in pushed_heads:
        if sha not in roots and len(roots) < ROOTS_MAX and _usable(repo, sha):
            roots.append(sha)
    ranges: list[tuple[str, str, str, int, set[str]]] = []  # (root, default base, its source, its moved, members)
    total = 0
    for r in roots:
        try:
            rb, rsrc, rmoved, _ = _default_base(repo, r, tip, pinned)
        except Unavailable:
            continue  # a root without a derivable base authorizes nothing
        members = {c.oid for c in repo.range_commits(rb, r)} | {r}
        total += len(members)
        if total > RANGE_COMMITS_MAX:
            raise nope  # a membership that cannot be established within the budget is refused
        ranges.append((r, rb, rsrc, rmoved, members))
    chosen = head if head is not None else head0
    if head is not None and not any(chosen in rng[-1] for rng in ranges):
        raise nope
    if base is None:
        return chosen, None, None, 0
    if base not in repo.ancestors(chosen):
        raise nope
    for _, rb, rsrc, rmoved, members in ranges:
        if chosen in members and base == rb:
            return chosen, base, rsrc, rmoved
    if base in repo.commit(chosen).parents[:1]:
        return chosen, base, "first_parent", 0
    if base in pushed_bases:
        return chosen, base, "pushed", 0
    raise nope


def diff(
    repo: Repo, branch: str, default_branch: str | None, context: int, max_bytes: int, ext: dict[str, Any] | None = None
) -> dict[str, Any]:
    repo.refs  # noqa: B018 (the ref store is checked as a whole first)
    head = repo.resolve_commit(_branch_ref(branch))
    if head is None:
        raise Unavailable(f"the branch {branch!r} does not exist")
    tip = None
    if default_branch:
        tip = repo.resolve_commit(_branch_ref(default_branch))
        if tip is None:  # review finding 4, hole 3: never a silent one-commit diff
            raise Unavailable(f"the declared default branch {default_branch!r} does not exist")
    else:
        for name in ("main", "master"):
            tip = repo.resolve_commit(_branch_ref(name))
            if tip is not None:
                break
    if ext is not None and not isinstance(ext, dict):
        raise Unavailable("bad request: ext")
    ext = ext or {}
    ident = None
    if ext:
        name_ = _str(ext, "name")
        assert name_ is not None
        urls = repo.config.origin_urls
        ident = canon.canonical_repo_identity(urls[0] if len(urls) == 1 else "", name_)  # F1 5.7, as client.identity_of
    pinned = None
    for p in _entries(ext.get("pins") or [], repo, "pins", 16):
        if ident is not None and _same(p["repo"], ident) and _usable(repo, p["sha"]):
            pinned = p["sha"]
            break
    head0 = head
    observed_base = repo.merge_base(head0, tip) if ext and tip is not None else None  # for the host to pin
    base_pick: str | None = None
    source, moved = "", 0
    if ext.get("since") is True:
        assert ident is not None
        anc = repo.ancestors(head)
        base_pick = next(
            (
                a["sha"]
                for a in _entries(ext.get("approvals") or [], repo, "approvals", 64)
                if _same(a["repo"], ident) and a["sha"] in anc
            ),
            None,
        )
        if base_pick is None:
            raise NotFound("no approval of yours covers this repo's history")
        source = "approved"
    elif ext.get("head") is not None or ext.get("base") is not None:
        assert ident is not None
        head, base_pick, src, moved = _authorize(repo, ext, ident, head, tip, pinned)
        source = src or ""
    if base_pick is not None:
        base = base_pick
    else:
        base, source, moved, _ = _default_base(repo, head, tip, pinned)
    rng = repo.range_commits(base, head)
    commits = [{"sha": c.oid, "subject": clean_line(c.subject)} for c in rng[: limits.COMMITS_SHOWN]]
    changes, files_cut = diff_trees(repo, repo.commit(base).tree, repo.commit(head).tree, limits.DIFF_FILES_MAX)
    parts: list[bytes] = []
    files: list[dict[str, Any]] = []
    size = 0
    text_cut = False
    used = 0
    for ch in changes:
        why, n = _too_large(repo, ch, used)
        used += n
        if why is None:
            text, fl, cut_one = render(repo, [ch], context, max_bytes)
            entry = {**fl[0], "too_large": False}
        else:
            text, cut_one = _marker(ch, why), False
            entry = {
                "path": ch.path.decode("utf-8", errors="replace"),
                "old_path": None,
                "status": ch.status,
                "add": 0,
                "del": 0,
                "binary": True,
                "too_large": True,
            }
        if size <= max_bytes:
            parts.append(text)
            size += len(text)
        else:
            text_cut = True
        text_cut = text_cut or cut_one
        files.append(entry)
    patch = b"".join(parts)
    cut = patch
    if len(patch) > max_bytes:
        nl = patch.rfind(b"\n", 0, max_bytes)
        cut = patch[: nl + 1] if nl >= 0 else b""
    return {
        "branch": branch,
        "base_sha": base,
        "base_source": source,
        "base_moved": moved,
        "observed_base": observed_base,
        "head_sha": head,
        "commits": commits,
        "commits_more": max(0, len(rng) - limits.COMMITS_SHOWN),
        "files": files,
        "patch": base64.b64encode(cut).decode("ascii"),
        "truncated": len(cut) < len(patch) or files_cut or text_cut,
        "origin_urls": repo.config.origin_urls,
        "object_format": repo.algo,
    }


def pin_many(args: dict[str, Any], roots: list[str]) -> dict[str, Any]:
    """``pin`` for each path of ``args["paths"]``, one answer per path, in order. A malformed request is refused as a
    whole; a path that cannot be pinned is refused on its own, with the code and message ``pin`` would answer, and
    every directory it opened is closed before the next path (:func:`pin_repo` closes its own)."""
    paths = args.get("paths")
    if set(args) != {"paths"} or not isinstance(paths, list) or not 0 < len(paths) <= limits.PIN_BATCH:
        raise Unavailable("bad request: paths")
    for i, p in enumerate(paths):
        _str({"path": p}, "path")
        if p in paths[:i]:
            raise Unavailable("bad request: paths")
    out: list[dict[str, Any]] = []
    for p in paths:
        try:
            out.append({"pins": pin_repo(p, roots).to_json()})
        except Unavailable as e:
            out.append({"error": {"code": e.code, "message": e.reason}})
        except PermissionError:  # the audit hook: as the child answers a single pin
            out.append({"error": {"code": Unavailable.code, "message": "the reader's profile refused an access"}})
    return {"results": out}


def records_identify(args: dict[str, Any], roots: list[str]) -> dict[str, Any]:
    """Is the ``.git`` at ``path`` THIS workspace's records repository?

    ``ours`` only when ALL hold: HEAD's commit is one the host itself recorded (``recorded``, from the host's
    ``records.json``, which an agent cannot write) and EVERY commit reachable from every ``refs/heads/*`` passes the
    records rules (:func:`orch.records.tool._headers_ok`, :func:`~orch.records.tool._message_ok` for ``workspace_id``).
    More than :data:`_HISTORY_MAX` commits, or anything unreadable, raises :class:`Unavailable` ("cannot tell").
    ``gitdir`` is the ``(dev, ino)`` of the identified ``.git`` directory, for the move to require."""
    path = _str(args, "path")
    wid = _str(args, "workspace_id")
    recorded = args.get("recorded")
    if (
        not isinstance(recorded, list)
        or len(recorded) > 256
        or not all(isinstance(r, str) and _HEX.fullmatch(r) for r in recorded)
    ):
        raise Unavailable("bad request: recorded")
    assert path is not None and wid is not None
    pins = pin_repo(path, roots)
    with Repo(pins) as repo:
        ident = [pins.gitdir.dev, pins.gitdir.ino]
        tip = repo.resolve_commit(repo.head_ref())
        if tip is None or tip not in set(recorded):
            return {"ours": False, "gitdir": ident}
        refs = {**repo.refs.packed, **repo.refs.loose}
        stack = list(refs.values())  # EVERY ref: code history cannot hide under tags, remotes, notes or the stash
        seen: set[str] = set()
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if len(seen) > _HISTORY_MAX:
                raise Unavailable("too many commits to identify the records repository")
            _kind, data = repo.objects.read(oid, "commit")
            head, _, message = data.partition(b"\n\n")
            names = [line.split(b" ", 1)[0].decode(errors="replace") for line in head.split(b"\n")]
            if not _headers_ok(names) or not _message_ok(message, wid):
                return {"ours": False, "gitdir": ident}
            stack.extend(repo.commit(oid).parents)
        return {"ours": True, "gitdir": ident}


WT_FILE_MAX = 1 << 20  # one side of one file in the live diff; larger is listed without hunks
WT_FILES_MAX = 200
_PROBE = 8000


def _head_entry(repo: Repo, tree: str, path: bytes) -> tuple[int, str] | None:
    """``(mode, oid)`` of ``path`` in the tree ``tree``, ``None`` when HEAD has no such blob."""
    cur: str | None = tree
    parts = path.split(b"/")
    for i, name in enumerate(parts):
        assert cur is not None
        hit = next((e for e in repo.tree(cur) if e.name == name), None)
        if hit is None:
            return None
        if i == len(parts) - 1:
            return None if hit.is_tree else (hit.mode, hit.oid)
        if not hit.is_tree:
            return None
        cur = hit.oid
    return None


def worktree_diff(
    repo: Repo,
    worktree: Any,
    *,
    max_bytes: int = 262144,
    max_files: int = WT_FILES_MAX,
    **check_kw: Any,
) -> dict[str, Any]:
    """The uncommitted changes of a working copy against ``HEAD`` (focus mode P2-5). The changed paths are the ones
    :mod:`orch.gitread.treecheck` finds (the same check ``dirty()`` uses); each file is read through the pinned
    working copy fd (``O_NOFOLLOW``), the ``HEAD`` side from the object store, and the hunks come from
    :mod:`orch.gitread.myers`. Binary, oversize, symlink and non-regular files are listed without hunks."""
    raw = treecheck.check(repo, worktree, **check_kw)
    head = raw.get("head")
    tree = repo.commit(head).tree if head else None
    files: list[dict[str, Any]] = []
    parts: list[bytes] = []
    size = 0
    truncated = bool(raw.get("more"))
    for c in raw["changed"]:
        if len(files) >= max_files:
            truncated = True
            break
        path = base64.b64decode(c["path_b64"])
        shown = escape_path(path, bool(c["cut"]))
        old = _head_entry(repo, tree, path) if tree and not c["cut"] else None
        rel = path.decode("utf-8", errors="surrogateescape")
        kind = None
        try:
            kind = worktree.kind(rel)
        except Unavailable:
            pass
        status = "?" if c["why"] == "untracked" else ("D" if kind is None else ("A" if old is None else "M"))
        entry: dict[str, Any] = {"path": shown, "status": status, "added": 0, "removed": 0, "binary": False}
        files.append(entry)
        a_data = b""
        if old is not None:
            if old[0] & 0o170000 != 0o100000:
                entry["binary"] = True
                continue
            a_data = repo.blob(old[1])
        b_data = b""
        if kind == "file":
            try:
                b_data = worktree.read(rel, WT_FILE_MAX)
            except Unavailable:
                entry["too_large"] = True
                continue
        elif kind is not None:  # a symlink, a directory in a file's place, a FIFO: never opened
            entry["binary"] = True
            continue
        if len(a_data) > WT_FILE_MAX:
            entry["too_large"] = True
            continue
        if b"\0" in a_data[:_PROBE] or b"\0" in b_data[:_PROBE]:
            entry["binary"] = True
            continue
        body, add, dele = myers.render(myers.split_lines(a_data), myers.split_lines(b_data), 3)
        entry["added"], entry["removed"] = add, dele
        q = quote_path(shown.encode("utf-8"))
        head_lines = [b"diff --git " + quote_path(b"a/" + shown.encode()) + b" " + quote_path(b"b/" + shown.encode())]
        if old is None:
            head_lines.append(b"new file mode %06o" % 0o100644)
        elif kind is None:
            head_lines.append(b"deleted file mode %06o" % old[0])
        head_lines.append(b"--- " + (b"/dev/null" if old is None else b"a/" + q))
        head_lines.append(b"+++ " + (b"/dev/null" if kind is None else b"b/" + q))
        text = b"\n".join(head_lines) + b"\n" + body if body else b"\n".join(head_lines[:1]) + b"\n"
        if size + len(text) > max_bytes:
            truncated = True
            continue
        parts.append(text)
        size += len(text)
    return {
        "head": head,
        "head_ref": raw.get("head_ref"),
        "files": files,
        "unified": b"".join(parts).decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


def run(profile: str, hello: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    op = req.get("op")
    args = req.get("args") or {}
    if op not in _OPS or profile not in _OPS[op] or not isinstance(args, dict):
        raise Unavailable(f"bad request: {op!r} under profile {profile!r}")
    if op == "pin":
        path = _str(args, "path")
        assert path is not None
        return {"pins": pin_repo(path, list(hello["roots"])).to_json()}
    if op == "pin_many":
        return pin_many(args, list(hello["roots"]))
    if op == "records_identify":
        return records_identify(args, list(hello["roots"]))
    pins = RepoPins.from_json(hello.get("pins"))
    with Repo(pins) as repo:
        if op == "tree_check":
            if set(args) - {"removal"} or type(args.get("removal", False)) is not bool:
                raise Unavailable("bad request: tree_check takes only removal (a boolean)")
            worktree = open_pin(pins.worktree)  # device and inode checked: still the pinned working copy
            try:
                # no person configuration here: the policy matches the repository's own ignore files in the case modes
                # its config allows (orch.gitread.ignorepolicy); a removal check never applies any (#583 round 3)
                return treecheck.check(repo, worktree, removal=bool(args.get("removal")))
            finally:
                worktree.close()
        if op == "worktree_diff":
            mb, mf = args.get("max_bytes", 262144), args.get("max_files", WT_FILES_MAX)
            if set(args) - {"max_bytes", "max_files"} or type(mb) is not int or not 1024 <= mb <= 1048576:
                raise Unavailable("bad request: worktree_diff")
            if type(mf) is not int or not 1 <= mf <= WT_FILES_MAX:
                raise Unavailable("bad request: worktree_diff")
            worktree = open_pin(pins.worktree)
            try:
                return worktree_diff(repo, worktree, max_bytes=mb, max_files=mf)
            finally:
                worktree.close()
        if op == "observe":
            refs = args.get("refs") or []
            if not isinstance(refs, list) or len(refs) > 64 or not all(isinstance(r, str) for r in refs):
                raise Unavailable("bad request: refs")
            return observe(repo, refs)
        context = args.get("context", 3)
        max_bytes = args.get("max_bytes", 262144)
        if type(context) is not int or not 0 <= context <= 10:
            raise Unavailable("bad request: context")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
            raise Unavailable("bad request: max_bytes")
        branch = _str(args, "branch")
        assert branch is not None
        ext = args.get("ext")
        if ext is not None and not isinstance(ext, dict):
            raise Unavailable("bad request: ext")
        return diff(repo, branch, _str(args, "default_branch", optional=True), context, max_bytes, ext)
