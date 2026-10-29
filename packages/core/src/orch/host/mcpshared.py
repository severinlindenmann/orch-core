"""Shapes shared by the company MCP proxy (host), the org state and the runner (M3 H-4).

* :func:`tool_pin`: what the owner turned On for one tool of a company MCP server. The pin covers EXACTLY the entry the
  agent is shown (every forwarded field: name, title, description, inputSchema), so upstream cannot change what the
  agent sees without the pin changing. The proxy serves a tool only while the definition upstream still has this pin
  (Opus host condition 8: a changed tool is Off and ``changed`` until the owner clicks again).
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

__all__ = ["CAPABILITY_SHAPE", "SERVER_NAME", "new_capability", "server_name_ok", "tool_pin"]

CAPABILITY_SHAPE = re.compile(r"ocap_[A-Za-z0-9_-]{43}")
SERVER_NAME = re.compile(r"[a-z0-9-]{1,40}")


def new_capability() -> str:
    return "ocap_" + secrets.token_urlsafe(32)


def server_name_ok(name: object) -> bool:
    return type(name) is str and SERVER_NAME.fullmatch(name) is not None and not name.startswith("orch")


def tool_pin(view: dict[str, Any]) -> str:
    """``sha256:<hex>`` of the canonical JSON (sorted keys, no spaces) of the tool entry exactly as it is forwarded to
    the agent. Build the forwarded entry first, then pin it: a field added to the forwarded entry is pinned with it."""
    body = json.dumps(view, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()
