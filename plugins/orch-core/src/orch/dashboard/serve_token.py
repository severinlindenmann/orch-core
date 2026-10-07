"""The dashboard's login token, kept per workspace so a restart of `orch serve` does not lock out open tabs and links.

It lives beside the ledger, in the permits folder of the orch config dir (`<config dir>/permits/dashboard/`), which
the guard keeps agents from reading or writing. The file is created 0600 and named after a hash of the workspace's
resolved path, never its name. A missing, unreadable or malformed file gets a fresh token; `orch serve --new-token`
replaces it on purpose (every browser that holds the old one is locked out).
"""
from __future__ import annotations

import hashlib
import re
import secrets
from pathlib import Path

from orch.core.fsutil import atomic_write_text, read_regular_file

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,64}")


def token_path(ws) -> Path:
    from orch.core.ledger import base_dir
    key = hashlib.sha256(str(ws.root.resolve()).encode("utf-8")).hexdigest()[:24]
    return base_dir() / "permits" / "dashboard" / f"{key}.token"


def read(ws) -> str | None:
    """The stored token, or None when there is none or it is not one orch wrote."""
    raw = read_regular_file(token_path(ws), 128)
    text = raw.decode("utf-8", "replace").strip() if raw else ""
    return text if _TOKEN_RE.fullmatch(text) else None


def load_or_create(ws, *, new: bool = False) -> str:
    """The workspace's token: the stored one, or a fresh one written first. A config dir that cannot be written still
    serves, with a token for this run only."""
    if not new:
        stored = read(ws)
        if stored:
            return stored
    token = secrets.token_urlsafe(24)
    path = token_path(ws)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(path, token + "\n")  # mkstemp: 0600
    except OSError:
        pass
    return token
