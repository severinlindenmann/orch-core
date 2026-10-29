"""What an agent must never copy out of the workspace (hub M3 spaces, SP-1 condition 3).

:func:`held_secrets` lists the secret values this host holds: the key files of its custody (``keys/*.filekey.json``),
the hub card keys, the space keys, every add-on credential (the GitHub token) and the grant the caller presented.
:func:`find` checks a plaintext against them and against the shapes that are secrets whatever their value (grant,
GitHub token, private key block): an exact substring, raw or as standard / url-safe base64. Best effort: a secret that
was split, re-encoded or typed by hand is not found. A value shorter than :data:`MIN_LEN` bytes is not checked (it
would match ordinary text)."""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import WorkspaceService

__all__ = ["MIN_LEN", "find", "held_secrets"]

MIN_LEN = 8
_PEM = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")


def _walk(doc: Any, keys: frozenset[str]) -> list[str]:
    out: list[str] = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k in keys and isinstance(v, str):
                out.append(v)
            else:
                out += _walk(v, keys)
    elif isinstance(doc, list):
        for v in doc:
            out += _walk(v, keys)
    return out


def _json(path: Path, host_uid: int) -> Any:
    from .custody import read_json

    try:
        return read_json(path, host_uid, limit=1 << 20)
    except Exception:  # noqa: BLE001 - an unreadable file holds nothing we can match; the op's own reads refuse it
        return None


def held_secrets(service: WorkspaceService, grant: str | None = None) -> list[bytes]:
    """Every secret value of this workspace's host, as bytes (the b64u text of a key AND its decoded bytes)."""
    from orch import crypto
    from orch.addons import credential

    state = Path(service.state_dir)
    ws_dir = state / "hosts" / service.workspace_id
    out: list[bytes] = []

    def add_text(text: str) -> None:
        out.append(text.encode())
        try:
            out.append(crypto.unb64u(text))
        except crypto.EncodingError:
            pass

    if grant:
        out.append(grant.encode())
    keys = ws_dir / "keys"
    if keys.is_dir():
        for path in sorted(keys.glob("*.filekey.json")):
            for text in _walk(_json(path, service.host_uid), frozenset({"d"})):
                add_text(text)
    for text in _walk(_json(ws_dir / "hub" / "card-keys.json", service.host_uid), frozenset({"wxk", "ck"})):
        add_text(text)
    spaces = _json(ws_dir / "hub" / "spaces.json", service.host_uid)
    if isinstance(spaces, dict):
        for slot in (spaces.get("spaces") or {}).values():
            for text in (slot.get("keys") or {}).values() if isinstance(slot, dict) else ():
                if isinstance(text, str):
                    add_text(text)
    root = credential.root_dir(state, service.workspace_id)
    if root.is_dir():
        for sub in sorted(root.iterdir()):
            try:
                secret = credential.load(state, service.workspace_id, service.host_uid, sub.name)
            except Exception:  # noqa: BLE001 - an unsafe credential file is refused elsewhere; skip it here
                continue
            if secret is not None:
                out.append(secret.reveal().encode())
    return [s for s in dict.fromkeys(out) if len(s) >= MIN_LEN]


def _variants(secret: bytes) -> list[bytes]:
    std, url = base64.b64encode(secret), base64.urlsafe_b64encode(secret)
    return [secret, std, std.rstrip(b"="), url, url.rstrip(b"=")]


def find(data: bytes, secrets: list[bytes]) -> bool:
    """Does ``data`` contain a held secret (raw or base64) or something shaped like a grant, a GitHub token or a
    private key?"""
    from orch.host.auth import _GRANT_BYTES
    from orch.tokenshape import GITHUB_TOKEN_BYTES

    if _GRANT_BYTES.search(data) or GITHUB_TOKEN_BYTES.search(data) or _PEM.search(data):
        return True
    return any(v in data for s in secrets for v in _variants(s))
