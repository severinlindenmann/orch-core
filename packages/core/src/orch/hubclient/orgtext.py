"""The text rules of a company skill (orch-hub ``contracts/hub/protocol.md`` 7.3, M3 #1430), the host's copy.

Company skills are read by agents, so text a person cannot see is a prompt-injection channel. The rule is an ALLOWLIST,
the same function the hub runs (``orch_hub.contract.org_bundles``): letters, numbers, punctuation, symbols and plain
spaces, plus newline and tab. Everything else is refused, never stripped. ``contracts/hub/vectors_org_skills.json``
(``text_cases``) holds the cases both sides replay, so they refuse identically."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any
from urllib.parse import urlsplit

__all__ = ["SKILL_TEXT_MAX", "mcp_problem", "skill_problem", "text_ok", "url_problem"]

SKILL_NAME = re.compile(r"[a-z][a-z0-9-]{1,39}")
RESERVED_PREFIX = "orch"  # orch's own skills; a company skill never shadows a builtin name
SKILL_TEXT_MAX = 2400
_ALLOWED_CATEGORIES = ("L", "N", "P", "S")
# letters or symbols by category that draw nothing (Hangul fillers, Khmer inherent vowels, braille blank)
_INVISIBLE_FILLERS = frozenset(chr(c) for c in (0x115F, 0x1160, 0x17B4, 0x17B5, 0x2800, 0x3164, 0xFFA0))


def text_ok(text: str) -> bool:
    return all(
        ch in "\n\t"
        or (
            ch not in _INVISIBLE_FILLERS
            and (unicodedata.category(ch)[0] in _ALLOWED_CATEGORIES or unicodedata.category(ch) == "Zs")
        )
        for ch in text
    )


def skill_problem(name: object, text: object) -> str | None:
    """None when a skill is acceptable, else a short reason code (same codes as the hub)."""
    if type(name) is not str or SKILL_NAME.fullmatch(name) is None:
        return "name"
    if name.startswith(RESERVED_PREFIX):
        return "reserved_name"
    if type(text) is not str or not text.strip():
        return "empty"
    if len(text) > SKILL_TEXT_MAX:
        return "too_long"
    if not text_ok(text):
        return "hidden_characters"
    if text.lstrip().startswith("---"):
        return "front_matter"
    return None


MCP_MAX = 20
URL_MAX = 200
DESCRIPTION_MAX = 200
AUTHS = ("none", "hub_secret")
SECRET_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")


def url_problem(url: Any) -> str | None:
    """A server is remote and public by name or address: https only, no credentials, query or fragment in the URL (a
    secret belongs in the secret store, never in a link), not this computer. A local command cannot
    be written at all."""
    if type(url) is not str or not url or len(url) > URL_MAX:
        return "url"
    if not url.startswith("https://"):
        return "https_only"
    if not url.isascii() or not url.isprintable() or " " in url:
        return "url"
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        return "url"
    host = u.hostname
    if (
        not host
        or u.username is not None
        or u.password is not None
        or u.query
        or u.fragment
        or "?" in url
        or "#" in url
    ):
        return "url"
    if port is not None and not 1 <= port <= 65535:
        return "url"
    if host == "localhost" or host.endswith(".localhost") or host.endswith("."):
        return "not_remote"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None if "." in host else "not_remote"  # a one-word host is this network's own box, not a service
    if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return "not_remote"
    return None


def mcp_problem(s: Any) -> str | None:
    """None when one server entry (without its digest) is acceptable, else a short reason code."""
    if type(s) is not dict or not {"name", "url", "auth", "secret", "description"} <= set(s):
        return "shape"
    name, auth, secret, desc = s["name"], s["auth"], s["secret"], s["description"]
    if type(name) is not str or SKILL_NAME.fullmatch(name) is None:
        return "name"
    if name.startswith(RESERVED_PREFIX):
        return "reserved_name"
    bad = url_problem(s["url"])
    if bad is not None:
        return bad
    if auth not in AUTHS:
        return "auth"
    if (auth == "none") != (secret is None) or (
        secret is not None and (type(secret) is not str or SECRET_NAME.fullmatch(secret) is None)
    ):
        return "secret"
    if type(desc) is not str or len(desc) > DESCRIPTION_MAX or "\n" in desc or not text_ok(desc):
        return "description"
    return None
