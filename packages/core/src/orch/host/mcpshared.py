"""Shapes shared by the company MCP proxy (host), the org state, the CLI and the runner (M3 H-4).

Invariant, enforced here and nowhere else: **approved view == pinned view == forwarded view**. :func:`tool_view` is the
ONLY place a server's tool definition is turned into the entry that is (a) stored and shown to the owner, (b) hashed
into the pin and (c) forwarded to the agent. Nothing is truncated anywhere: a definition that does not fit the limits
has no view (only ``{"name", "problem": "too_long"}``), so it can never be pinned, turned On or forwarded.

* :func:`plain`: every server-controlled string that is shown anywhere goes through it (no control characters).
* :func:`tool_view`: the one builder of the view (name, title if any, description, inputSchema).
* :func:`tool_pin`: what the owner turned On for one tool of a company MCP server: sha256 of the view. The proxy serves
  a tool only while the definition upstream still has this pin (Opus host condition 8: a changed tool is Off and
  ``changed`` until the owner clicks again).
* :data:`CAPABILITY_SHAPE`: a per-run proxy capability (``ocap_`` and 43 url-safe characters, 256 random bits). Log
  filters replace anything of this shape (condition 4: never logged).
* :data:`SERVER_NAME`: a company MCP server name as it appears in an agent's config (``[a-z0-9-]{1,40}``, never
  starting with ``orch``, condition 7).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any

from orch.hubclient.orgtext import text_ok

__all__ = [
    "CAPABILITY_ANY",
    "CAPABILITY_SHAPE",
    "SERVER_NAME",
    "TOOL_DESCRIPTION_MAX",
    "TOOL_NAME",
    "TOOL_SCHEMA_MAX",
    "TOOL_TITLE_MAX",
    "new_capability",
    "plain",
    "server_name_ok",
    "tool_pin",
    "tool_view",
]

CAPABILITY_SHAPE = re.compile(r"(?<![A-Za-z0-9_-])ocap_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
#: for REDACTING only (log/text substitution): no boundaries, so a capability glued to other characters is still hidden;
#: over-redacting harmless text costs nothing. Refusals and exact checks use the bounded :data:`CAPABILITY_SHAPE`.
CAPABILITY_ANY = re.compile(r"ocap_[A-Za-z0-9_-]{43}")
SERVER_NAME = re.compile(r"[a-z0-9-]{1,40}")
TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")
TOOL_TITLE_MAX = 100
TOOL_DESCRIPTION_MAX = 500
TOOL_SCHEMA_MAX = 16384  # bytes of canonical JSON
_DEPTH_MAX = 24
_CONTROL = {c: None for c in [*range(0x20), *range(0x7F, 0xA0)]}
_CONTROL.update({0x0A: "\n", 0x09: " "})


def plain(text: str) -> str:
    """``text`` without any C0/C1 control character (ESC included); a tab becomes a space, a newline stays."""
    return text.translate(_CONTROL)


def new_capability() -> str:
    return "ocap_" + secrets.token_urlsafe(32)


def server_name_ok(name: object) -> bool:
    return type(name) is str and SERVER_NAME.fullmatch(name) is not None and not name.startswith("orch")


def tool_pin(view: dict[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON (sorted keys, no spaces) of a :func:`tool_view`. Only a view without a
    problem is ever pinned; the entry shown to the owner and the entry forwarded to the agent are this same dict."""
    body = json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _clean(obj: Any, depth: int = 0) -> Any:
    """``obj`` with ``plain`` applied to every string (keys too); ValueError when not plain JSON or too deep."""
    if depth > _DEPTH_MAX:
        raise ValueError("deep")
    if type(obj) is str:
        return plain(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if type(k) is not str:
                raise ValueError("key")
            out[plain(k)] = _clean(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_clean(v, depth + 1) for v in obj]
    if obj is None or type(obj) in (bool, int, float):
        return obj
    raise ValueError("type")


def _texts_ok(obj: Any) -> bool:
    if type(obj) is str:
        return text_ok(obj)
    if isinstance(obj, dict):
        return all(text_ok(k) and _texts_ok(v) for k, v in obj.items())
    if isinstance(obj, list):
        return all(_texts_ok(v) for v in obj)
    return True


def tool_view(raw: object) -> tuple[dict[str, Any] | None, str | None]:
    """``(view, problem)`` for one tool definition from a company server.

    ``view`` is the canonical entry ``{"name", "description", "inputSchema"}`` (+ ``"title"`` when non-empty) with
    :func:`plain` applied to every string. Problems: ``"shape"`` (not a tool: no view), ``"text"`` (hidden or odd
    characters in any text, also in outputSchema or annotations: no view), ``"too_long"`` (a limit is passed: the view
    is ``{"name": name}`` only, nothing is cut). A tool with a problem is never pinned."""
    if not isinstance(raw, dict):
        return None, "shape"
    name = raw.get("name")
    if type(name) is not str or TOOL_NAME.fullmatch(name) is None:
        return None, "shape"
    description, title, schema = raw.get("description"), raw.get("title"), raw.get("inputSchema", {"type": "object"})
    if description is None:
        description = ""
    if type(description) is not str or (title is not None and type(title) is not str) or not isinstance(schema, dict):
        return None, "shape"
    try:
        schema = _clean(schema)
    except (ValueError, RecursionError):
        return None, "shape"
    description, title = plain(description), plain(title or "")
    if not (text_ok(description) and text_ok(title) and _texts_ok(schema)):
        return None, "text"
    for extra in ("outputSchema", "annotations"):
        value = raw.get(extra)
        if value is not None and (not isinstance(value, dict) or not _texts_ok(value)):
            return None, "text"
    try:
        size = len(json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    except (TypeError, ValueError):
        return None, "shape"
    if len(description) > TOOL_DESCRIPTION_MAX or len(title) > TOOL_TITLE_MAX or size > TOOL_SCHEMA_MAX:
        return {"name": name}, "too_long"
    view: dict[str, Any] = {"name": name, "description": description, "inputSchema": schema}
    if title:
        view["title"] = title
    return view, None
