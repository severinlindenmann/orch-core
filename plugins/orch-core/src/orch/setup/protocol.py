"""The wire of ``<state>/setup/setup.sock`` and the closed profile schema (batch-9.md "## Contract").

HTTP/1.1 over the unix socket, JSON bodies, errors ``{"error": {"code", "message"}}`` (codes, never free text from a
caller). Browser routes live under ``/api/setup/`` (the dashboard proxies them unchanged and adds nothing but the
``X-Setup-Token`` the browser holds); the three socket-only routes (``/launch``, ``/claim``, ``/shutdown``) have no such
prefix, so a proxy that forwards ``/api/setup/*`` can never reach them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

__all__ = [
    "AGENT_HOURS",
    "BROWSER_PREFIX",
    "MAX_BODY",
    "METHODS",
    "PHASES",
    "SOCKET_ROUTES",
    "SetupError",
    "validate_profile",
]

BROWSER_PREFIX = "/api/setup/"
SOCKET_ROUTES = frozenset({"/launch", "/claim", "/shutdown"})
MAX_BODY = 64 * 1024
PHASES = ("profile", "secrets", "review", "confirming", "running", "done", "failed", "cancelled")
METHODS = ("terminal", "click")
#: hours an agent may work: 8 h, 1, 3 or 7 days (B8 caps agent access at 7 days)
AGENT_HOURS = (8, 24, 72, 168)

_PREFIX = re.compile(r"[A-Z][A-Z0-9]{1,7}")
_ADDON_ID = re.compile(r"[a-z][a-z0-9-]{0,39}")
_PROFILE_KEYS = frozenset(
    {"name", "workspace_name", "prefix", "addons", "method", "agent_hours", "agent_grant", "relay"}
)
#: keys a page may leave out (their value then is the default)
_OPTIONAL_KEYS = frozenset({"git_init", "records"})


class SetupError(Exception):
    """A refusal with a stable code; the server renders it as ``{error:{code,message}}`` with ``status``."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


def clean_name(value: Any) -> str:
    """One line of printable text (no control or format characters), NFC, whitespace collapsed; ``invalid.input``
    otherwise. The same rule as ``orch setup``'s owner name."""
    if not isinstance(value, str):
        raise SetupError("invalid.input", "name is text", field="name")
    name = unicodedata.normalize("NFC", " ".join(value.split()))
    if not 1 <= len(name) <= 80 or any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn") for c in name):
        raise SetupError("invalid.input", "name is 1 to 80 printable characters", field="name")
    return name


def validate_profile(
    body: Any, *, known_addons: frozenset[str], required: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """The closed schema of ``POST profile``: exactly these keys, each of its type; unknown keys are refused. Returns
    the normalised profile. R5 (click + relay) is the caller's: it needs the click availability."""
    if not isinstance(body, dict):
        raise SetupError("invalid.input", "the profile is an object", field="profile")
    extra = set(body) - _PROFILE_KEYS - _OPTIONAL_KEYS
    if extra:
        raise SetupError("invalid.input", f"unknown field {sorted(extra)[0][:40]!r}", field="profile")
    missing = _PROFILE_KEYS - set(body)
    if missing:
        raise SetupError("invalid.input", f"missing field {sorted(missing)[0]}", field=sorted(missing)[0])
    name = clean_name(body["name"])
    workspace_name = clean_name(body["workspace_name"])
    prefix = body["prefix"]
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise SetupError(
            "invalid.input", "prefix is 2 to 8 capital letters or digits, starting with a letter", field="prefix"
        )
    addons = body["addons"]
    if (
        not isinstance(addons, list)
        or len(addons) > 32
        or any(not isinstance(a, str) or not _ADDON_ID.fullmatch(a) for a in addons)
        or len(set(addons)) != len(addons)
    ):
        raise SetupError("invalid.input", "addons is a list of distinct add-on ids", field="addons")
    unknown = [a for a in addons if a not in known_addons]
    if unknown:
        raise SetupError("invalid.input", f"unknown add-on {unknown[0]}", field="addons")
    if not required <= set(addons):
        raise SetupError("invalid.input", f"{sorted(required - set(addons))[0]} is required", field="addons")
    if body["method"] not in METHODS:
        raise SetupError("invalid.input", "method is terminal or click", field="method")
    hours = body["agent_hours"]
    if type(hours) is not int or hours not in AGENT_HOURS:
        raise SetupError("invalid.input", f"agent_hours is one of {list(AGENT_HOURS)}", field="agent_hours")
    if type(body["agent_grant"]) is not bool:
        raise SetupError("invalid.input", "agent_grant is true or false", field="agent_grant")
    if type(body["relay"]) is not bool:
        raise SetupError("invalid.input", "relay is true or false", field="relay")
    git_init = body.get("git_init", False)
    if type(git_init) is not bool:
        raise SetupError("invalid.input", "git_init is true or false", field="git_init")
    records = body.get("records", False)  # consent to the records repo + local auto-commit; absent means no
    if type(records) is not bool:
        raise SetupError("invalid.input", "records is true or false", field="records")
    return {
        "name": name,
        "workspace_name": workspace_name,
        "prefix": prefix,
        "addons": sorted(addons),
        "method": body["method"],
        "agent_hours": hours,
        "agent_grant": body["agent_grant"],
        "relay": body["relay"],
        "git_init": git_init,
        "records": records,
    }
