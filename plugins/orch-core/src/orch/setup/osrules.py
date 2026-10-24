"""Pure parsers for the two root-owned rule sets that could hand an agent user root or a login: sudoers and sshd.

Used by the root-time setup plan (which can read both and may run ``sudo -l -U`` / ``sshd -T``) and by the
unprivileged ``isolation.detect`` (which usually cannot read them). The parsers are conservative on purpose: a line that
*might* grant the user counts as granting it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from fnmatch import fnmatch

__all__ = ["sshd_denies", "sudoers_logical_lines", "sudoers_names"]

_ALIAS_KINDS = ("cmnd_alias", "host_alias", "runas_alias")


def sudoers_logical_lines(text: str) -> list[str]:
    """Join continuations, drop comments. A ``#`` followed by a digit (``#1000``) or ``include`` is NOT a comment."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = pending + raw.strip()
        pending = ""
        if line.endswith("\\"):
            pending = line[:-1] + " "
            continue
        if not line:
            continue
        if line.startswith("#") and not re.match(r"#(\d|include)", line):
            continue
        line = re.sub(r"\s#(?!\d)(?!include).*$", "", line)  # trailing comment
        out.append(line.strip())
    if pending.strip():
        out.append(pending.strip())
    return out


def _tokens(text: str) -> list[str]:
    return [t.strip() for t in re.split(r"[,\s]+", text) if t.strip()]


def sudoers_names(
    lines: list[str],
    user: str,
    uid: int | None = None,
    gids: frozenset[int] = frozenset(),
    groups: frozenset[str] = frozenset(),
) -> list[str]:
    """Active sudoers lines that give ``user`` anything: the name, ``#uid``, ``%group``, ``%#gid``, ``ALL`` or a
    ``User_Alias`` that contains one of them. Negated entries (``!x``) never match."""
    wanted = {user, "ALL"}
    if uid is not None:
        wanted.add(f"#{uid}")
    wanted |= {f"%{user}"} | {f"%{g}" for g in groups} | {f"%#{g}" for g in gids}
    aliases: dict[str, list[str]] = {}
    for line in lines:
        m = re.match(r"user_alias\s+(.*)$", line, re.I)
        if m:
            for part in m.group(1).split(":"):
                if "=" in part:
                    name, members = part.split("=", 1)
                    aliases.setdefault(name.strip(), []).extend(_tokens(members))
    matching: set[str] = set()
    changed = True
    while changed:  # an alias may contain another alias
        changed = False
        for name, members in aliases.items():
            if name not in matching and any(t in wanted or t in matching for t in members if not t.startswith("!")):
                matching.add(name)
                changed = True
    hits: list[str] = []
    for line in lines:
        low = line.lower()
        if low.startswith(("defaults", "user_alias")) or any(low.startswith(k) for k in _ALIAS_KINDS):
            continue
        if low.startswith(("@include", "#include")):
            continue  # inlined by the reader; a directive that survives is not a grant
        lhs = line.split("=", 1)[0].rstrip()
        parts = lhs.rsplit(None, 1)
        if len(parts) != 2:
            continue
        users = [t for t in _tokens(parts[0]) if not t.startswith("!")]
        if any(t in wanted or t in matching for t in users):
            hits.append(line)
    return hits


def sshd_denies(
    read: Callable[[str], str | None],
    listdir: Callable[[str], list[str]],
    name: str,
    main: str = "/etc/ssh/sshd_config",
) -> bool:
    """Whether the active (non-``Match``) config denies ``name``: ``DenyUsers`` lines, following only active
    ``Include`` directives, and ignoring everything from the first ``Match`` on (that is conditional). OSError from
    ``read`` propagates (unreadable = unknown)."""
    state = {"match": False, "deny": False}

    def parse(path: str, depth: int) -> None:
        if depth > 8 or state["match"]:
            return
        text = read(path)
        if text is None:
            return
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if not parts:
                continue
            key = parts[0].lower()
            if key == "match":
                state["match"] = True
                return
            if key == "include":
                for pat in parts[1:]:
                    pat = pat if pat.startswith("/") else f"/etc/ssh/{pat}"
                    folder, base = os.path.split(pat)
                    for entry in sorted(listdir(folder)):
                        if fnmatch(entry, base):
                            parse(f"{folder}/{entry}", depth + 1)
                            if state["match"]:
                                return
            elif key == "denyusers" and any(fnmatch(name, p) for p in parts[1:]):
                state["deny"] = True

    parse(main, 0)
    return state["deny"]
