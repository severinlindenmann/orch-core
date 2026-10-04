"""`documents:` globs (ruling R13): repo-relative, `*` within a folder, `?` one character, `**` any depth,
a trailing `/` means everything below, and an optional `<repo>:` prefix limits the glob to that git.repos entry."""
from __future__ import annotations

import functools
import re

_REPO = re.compile(r"[A-Za-z0-9_.-]+")


def normalize(path: str) -> str:
    p = str(path).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def split_repo(pattern: str) -> tuple[str | None, str]:
    head, sep, rest = pattern.partition(":")
    if sep and rest and "/" not in head and _REPO.fullmatch(head):
        return head, rest
    return None, pattern


@functools.lru_cache(maxsize=1024)
def _compiled(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def glob_match(pattern: str, path: str, repo: str | None = None) -> bool:
    want, glob = split_repo(pattern)
    if want is not None and repo is not None and want != repo:
        return False
    glob = normalize(glob)
    if glob.endswith("/"):
        glob += "**"
    return _compiled(glob).fullmatch(normalize(path)) is not None
