"""Search for the explorer (issue #491): file names in the browser's help, and text inside files.

Content search is LITERAL (no regular expression: nothing a person types can stall the search). It uses `rg` when
it is installed, else `git grep` in the root's own repository (the saved/staged content, not unsaved edits). Both are
capped (hits, bytes, seconds) and run in their own process group. **Whatever the tool says, every hit is checked again
by the guard**: the path must be plain, not denied, and a regular file with no link in any component. A hit that fails
is dropped. ripgrep never follows links (no `-L`), reads no config (`--no-config`), runs no program (`--pre` is not
used) and gets the deny list as `--glob` exclusions.
"""

from __future__ import annotations

import os
import shutil

from orch import gitbrowse, gitsafe

from ..errors import HostError
from . import describe, guard
from . import git as history

MAX_HITS = 200
MAX_BYTES = 1_000_000
SECONDS = 10.0
#: heavy folders the file-name walk does not enter
SKIP = frozenset({"node_modules", ".venv", "__pycache__", ".cache", ".next", ".tox"})
WALK_LIMIT = 20_000


def rg_path() -> str | None:
    return shutil.which("rg", path=os.environ.get("PATH", "/usr/bin:/bin"))


def tools() -> dict:
    return {"rg": rg_path() is not None, "git": shutil.which("git") is not None}


def _clean_hit(root: guard.Root, rel: str, line: int, text: str) -> dict | None:
    """The page's row for one hit, or None when the guard refuses the path."""
    try:
        parts = guard.split(rel)
        if not parts or guard.deny(parts):
            return None
        st = root.stat_file(parts)  # no link in any component, not denied, present
    except guard.GuardError:
        return None
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        return None
    return {
        "root": root.id,
        "path": "/".join(parts),
        "label": guard.label("/".join(parts)),
        "line": line,
        "text": describe.clean_line(text),
    }


def _rg(root: guard.Root, folder: tuple[str, ...], query: str, budget: int):
    exe = rg_path()
    argv = [
        exe, "--no-config", "--fixed-strings", "--smart-case", "--line-number", "--no-heading", "--with-filename",
        "--null", "--color", "never", "--max-columns", "400", "--max-columns-preview", "--max-filesize", "2M",
        "--max-count", "5", "--no-messages", "--no-require-git", "--no-ignore-global", "--no-ignore-parent",
        "--max-count", "5",
    ]  # fmt: skip
    for g in guard.exclude_globs():
        argv += ["--glob", g]
    argv += ["--glob", "!node_modules", "--glob", "!node_modules/**", "-e", query, "--", "."]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "HOME": os.environ.get("HOME", "/")}
    with root.dir(folder) as fd:  # the folder exists, is not denied, no part of it is a link; the child starts IN it
        res = gitbrowse.spawn(argv, env, cwd_fd=fd, max_bytes=MAX_BYTES, timeout=SECONDS)
    hits = []
    for chunk in res.out.split(b"\n"):
        path, sep, rest = chunk.partition(b"\0")
        num, colon, text = rest.partition(b":")
        if not sep or not colon or not num.isdigit():
            continue
        rel = path.decode("utf-8", "replace")
        rel = rel[2:] if rel.startswith("./") else rel
        hits.append((rel, int(num), text.decode("utf-8", "replace")))
        if len(hits) >= budget:
            break
    return hits, res.cut or res.timed_out or len(hits) >= budget


def _git(root: guard.Root, folder: tuple[str, ...], query: str, budget: int):
    try:
        repo = history.locate(root, ("x",))  # the repository AT the root (confirmed), or none
    except HostError:
        return [], False
    if repo is None or repo.prefix:
        return [], False
    try:
        res = gitbrowse.run(repo.folder, "grep", os.environ, operand=(query,),
                            max_bytes=MAX_BYTES, timeout=SECONDS)  # fmt: skip
    except gitsafe.Unsafe:
        return [], False
    prefix = "/".join(folder) + "/" if folder else ""
    rows = [(p, n, t) for p, n, t in gitbrowse.parse_grep(res.out) if p.startswith(prefix)] if res.code == 0 else []
    return rows[:budget], res.cut or len(rows) > budget


def search(roots: list[guard.Root], folder: tuple[str, ...], query: str) -> dict:
    """Literal content search over ``roots`` (below ``folder`` when given, only with one root). ``{engine, hits,
    more}``; ``engine`` None when no search tool exists."""
    engine = "rg" if rg_path() else ("git" if tools()["git"] else None)
    if engine is None:
        return {"engine": None, "hits": [], "more": False}
    out: list[dict] = []
    more = False
    if folder:  # a folder to look in must be a real, unlocked folder (`Denied`/`Missing` reach the caller)
        for root in roots:
            with root.dir(folder):
                pass
    for root in roots:
        budget = MAX_HITS - len(out)
        if budget <= 0:
            more = True
            break
        try:
            raw, cut = (_rg if engine == "rg" else _git)(root, folder, query, budget + 1)
        except (guard.GuardError, gitsafe.Unsafe):
            continue
        more = more or cut
        for rel, line, text in raw:
            if (hit := _clean_hit(root, rel, line, text)) is not None:
                out.append(hit)
    if len(out) > MAX_HITS:
        out, more = out[:MAX_HITS], True
    return {"engine": engine, "hits": out, "more": more}


def _score(query: str, path: str) -> int | None:
    """Lower is better: 0 the name equals, 1 starts with, 2 contains (name), 3 contains (path), 4 letters in order."""
    q, p = query.casefold(), path.casefold()
    name = p.rsplit("/", 1)[-1]
    if name == q:
        return 0
    if name.startswith(q):
        return 1
    if q in name:
        return 2
    if q in p:
        return 3
    it = iter(p)
    return 4 if all(c in it for c in q) else None


def names(root: guard.Root, folder: tuple[str, ...], query: str, limit: int = 100) -> dict:
    """File names matching ``query``, best first: ``{files, cut}``; ``cut``: the walk stopped at :data:`WALK_LIMIT`."""
    found, cut = root.walk_names(folder, WALK_LIMIT, SKIP)
    scored = []
    for parts in found:
        path = "/".join(parts)
        score = _score(query, path)
        if score is not None:
            scored.append((score, len(path), path, parts))
    scored.sort()
    files = [{"root": root.id, "path": p, "label": guard.label(p)} for _s, _l, p, _parts in scored[:limit]]
    return {"files": files, "cut": cut}
