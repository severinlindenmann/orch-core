"""A concrete request path for every route, for the sweeps that call each route once. A path that the router would not
match (it answers ``unknown_op``) makes a sweep pass without testing anything, so this picks, per route, the first
candidate value for each ``{param}`` such that the WHOLE path matches the route's own compiled pattern, and raises
loudly when none does (a new param shape must be added here on purpose)."""

from __future__ import annotations

import re
from typing import Any

CANDIDATES = ("ab" * 16, "ab" * 8, "A1" * 13, "relay", "hub", "a" * 32, "x")
_PARAM = re.compile(r"\{(\w+)\}")


def concrete_path(rt: Any, overrides: dict[str, str] | None = None) -> str:
    """The route's path (with the ``/api/v1`` prefix) with every ``{name}`` filled in; it fullmatches ``rt.rx``."""
    overrides = overrides or {}
    path = rt.path
    for name in dict.fromkeys(_PARAM.findall(rt.path)):
        if name in overrides:
            path = path.replace("{" + name + "}", overrides[name])
            continue
        for cand in CANDIDATES:
            trial = path.replace("{" + name + "}", cand)
            if re.fullmatch(_group_pattern(rt, name), cand):
                path = trial
                break
        else:
            raise AssertionError(
                f"route {rt.method} {rt.path}: no candidate value fits {{{name}}}; add one to CANDIDATES"
            )
    if not rt.rx.fullmatch(path):
        raise AssertionError(f"route {rt.method} {rt.path}: {path!r} does not match the route's own pattern")
    return path


def _group_pattern(rt: Any, name: str) -> str:
    m = re.search(rf"\(\?P<{name}>(.*?)\)(?=/|$|\))", rt.rx.pattern)
    if m is None:
        raise AssertionError(f"route {rt.method} {rt.path}: no pattern for {{{name}}}")
    return m.group(1)
