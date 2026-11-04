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
    store = ws_dir / "membership" / "keys"  # WK_e (relay epoch keys), the WXK scalar, CK_s: wk-*, wxk-*, ck-*
    if store.is_dir():
        for path in sorted(store.glob("*.json")):
            if path.name.startswith(("wk-", "wxk-", "ck-")):
                for text in _walk(_json(path, service.host_uid), frozenset({"wk", "d", "ck"})):
                    add_text(text)
    for text in _walk(_json(ws_dir / "hub" / "card-keys.json", service.host_uid), frozenset({"wxk", "ck"})):
        add_text(text)
    for text in _walk(_json(ws_dir / "remote" / "wrk.json", service.host_uid), frozenset({"wrk"})):
        add_text(text)  # R2 #1590: the remote access key (WRK), host custody only
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


_RUN_B64 = re.compile(rb"[A-Za-z0-9+/_-]{16,}={0,2}")
_RUN_HEX = re.compile(rb"(?:[0-9a-fA-F]{2}){8,}")


def _variants(secret: bytes) -> list[bytes]:
    """Raw; base64 (standard and url-safe) at each of the three alignments, only the part that does not depend on the
    neighbouring bytes (so it matches wherever the secret sits); hex, lower and upper. Never a form under MIN_LEN."""
    out = [secret, secret.hex().encode(), secret.hex().upper().encode()]
    for k in (0, 1, 2):
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            text = enc(b"\0" * k + secret).rstrip(b"=")
            head = (0, 2, 3)[k]  # chars that still hold bits of the padding bytes
            tail = 0 if (len(secret) + k) % 3 == 0 else 1  # the last char holds bits of what follows
            text = text[head : len(text) - tail]
            if k == 0:
                out.append(enc(secret))  # the whole padded form too
            out.append(text)
    return [v for v in dict.fromkeys(out) if len(v) >= MIN_LEN]


def _decoded(data: bytes) -> list[bytes]:
    """Base64 and hex runs of ``data`` decoded (an agent may encode a secret before it writes it)."""
    out = []
    for m in _RUN_HEX.finditer(data):
        out.append(bytes.fromhex(m.group().decode()))
    for m in _RUN_B64.finditer(data):
        run = m.group().rstrip(b"=")
        for pad in (0, 1, 2, 3):  # a run cut at a boundary decodes after a few characters are dropped
            chunk = run[pad:]
            chunk = chunk[: len(chunk) - len(chunk) % 4] if len(chunk) % 4 == 1 else chunk
            try:
                out.append(base64.b64decode(chunk.replace(b"-", b"+").replace(b"_", b"/") + b"=" * (-len(chunk) % 4)))
            except ValueError:
                continue
    return out


def find(data: bytes, secrets: list[bytes]) -> bool:
    """Does ``data`` contain a held secret (raw, base64 at any alignment, or hex), also inside a base64 or hex run, or
    something shaped like a grant, a GitHub token, a proxy capability or a private key?"""
    from orch.host.auth import _GRANT_BYTES
    from orch.host.mcpshared import CAPABILITY_ANY
    from orch.tokenshape import GITHUB_TOKEN_BYTES

    def shaped(d: bytes) -> bool:
        return bool(
            _GRANT_BYTES.search(d)
            or GITHUB_TOKEN_BYTES.search(d)
            or _PEM.search(d)
            or CAPABILITY_ANY.search(d.decode("latin-1"))
        )

    variants = [v for s in secrets for v in _variants(s)]
    for d in (data, *_decoded(data)):
        if shaped(d) or any(v in d for v in variants):
            return True
    return False
