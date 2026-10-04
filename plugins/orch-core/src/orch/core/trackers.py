"""External tracker keys (spec v2 §13.3). A tracker is {prefix, pattern, url}. The pattern may hold a named group
(?P<id>…) and the URL may use {id} (that group) besides {key} (the whole key), e.g. GH-(?P<id>\\d+) with
https://github.com/acme/app/issues/{id}. Without the group, {id} is the whole key, so old configs behave as before."""
from __future__ import annotations

import re
from urllib.parse import quote

_NAMED_GROUP = re.compile(r"\(\?P<[A-Za-z_][A-Za-z0-9_]*>")
_BARE_NUMBERS = ("1", "12", "1234")
# http(s), a host without placeholders, then anything: {key}/{id} may only come after the host
_URL_TEMPLATE = re.compile(r"https?://[^/?#{}\s]+(?:[/?#]\S*)?", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{(key|id)\}")


def plain(pattern: str) -> str:
    """`pattern` with its named groups turned into non-capturing ones, so several trackers can be OR-ed into one
    regex (two trackers that both use (?P<id>…) would otherwise redefine the group and crash)."""
    return _NAMED_GROUP.sub("(?:", pattern)


def _fullmatch(pattern, key):
    try:
        return re.fullmatch(pattern, str(key).strip(), re.IGNORECASE)
    except (re.error, TypeError):
        return None


def matches(pattern, key) -> bool:
    return _fullmatch(pattern, key) is not None


def accepts_bare_number(pattern) -> bool:
    """True for a pattern such as \\d+ that matches a plain number: it would match any number in free text, so a
    tracker needs a prefix (`orch migrate` adds it to old configs)."""
    return any(matches(pattern, n) for n in _BARE_NUMBERS)


def find(tracker_list, key) -> dict | None:
    for t in tracker_list or []:
        if isinstance(t, dict) and isinstance(t.get("pattern"), str) and matches(t["pattern"], key):
            return t
    return None


def url_for(tracker: dict, key) -> str | None:
    """The tracker URL for `key`: {key} and {id} replaced in one pass with percent-encoded values (so a value can
    never inject another placeholder, a path or a query). None for a template that is not http(s)."""
    m = _fullmatch(tracker.get("pattern", ""), key)
    url = tracker.get("url")
    if m is None or not isinstance(url, str) or not _URL_TEMPLATE.fullmatch(url):
        return None
    key = str(key).strip()
    values = {"key": key, "id": m.group("id") if "id" in m.re.groupindex and m.group("id") is not None else key}
    return _PLACEHOLDER.sub(lambda p: quote(values[p.group(1)], safe=""), url)


def external_ref(tracker_list, key: str) -> dict:
    """{key, url}: upper-cased with a URL when a tracker matches, else the key as given without a URL."""
    key = key.strip()
    t = find(tracker_list, key)
    if t is None:
        return {"key": key, "url": None}
    key = key.upper()
    return {"key": key, "url": url_for(t, key)}


def _has_backreference(pattern: str) -> bool:
    """\\1 … \\9 or (?P=name), skipping escaped backslashes. plain() and OR-ing renumber or drop the groups they
    point at, so a backreference would break the commit check."""
    if "(?P=" in pattern:
        return True
    i = 0
    while i < len(pattern) - 1:
        if pattern[i] == "\\":
            if pattern[i + 1] in "123456789":
                return True
            i += 2
        else:
            i += 1
    return False


def tracker_problem(tracker: dict) -> str | None:
    pattern, url = tracker.get("pattern"), tracker.get("url")
    try:
        compiled = re.compile(pattern)
    except (re.error, TypeError) as e:
        return f"pattern {pattern!r} is not a valid regex ({e})"
    if accepts_bare_number(pattern):
        return (f"pattern {pattern!r} matches a bare number; give it a prefix such as GH-(?P<id>\\d+) "
                "(`orch migrate` does this for an old config that has a \"prefix\")")
    if _has_backreference(pattern):
        return f"pattern {pattern!r} uses a backreference (\\1 or (?P=…)), which cannot be combined with other trackers"
    try:
        re.compile(f"(?P<key>(?:{plain(pattern)}))")  # the form the commit check and orch check use
    except re.error as e:
        return f"pattern {pattern!r} is not a valid regex once combined with other trackers ({e})"
    if not isinstance(url, str) or ("{key}" not in url and "{id}" not in url):
        return "url must contain {key} or {id}"
    if not _URL_TEMPLATE.fullmatch(url):
        return "url must start with http:// or https:// and a host; {key} and {id} may only come after the host"
    if "{id}" in url and "id" not in compiled.groupindex:
        return "url uses {id}, so the pattern needs a named group (?P<id>…), e.g. GH-(?P<id>\\d+)"
    return None
