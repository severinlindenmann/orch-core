"""Paired phones per workspace (spec §6.4). The key lives only in this file and on the paired phone.
Owner-only (0600: written through `atomic_write_text`, whose temp file `mkstemp` creates 0600); the guard denies
agents reading or writing it. Pair, revoke and the permissions are human-only dashboard POSTs."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from orch.addons.userfiles import read_json_object, update_json, workspace_key
from orch.clock import stamp_s
from orch.errors import UsageError

# What a phone may decide. `move` is never here (P0 ruling): a phone never moves a ticket on its own.
KINDS = ("answer", "request_changes", "approve", "verdict", "ticket_request")
# R21: a paired phone is the owner, so every kind applies at once by default; the owner may turn a kind off.
DEFAULT_PERMISSIONS = {k: True for k in KINDS}
# What the Phones form saved before R21 when the owner never changed a switch (no "v" marker): read as untouched.
_OLD_DEFAULTS = {"answer": True, "request_changes": True, "approve": False, "verdict": False}
PERMISSIONS_V = 2
_LABEL = re.compile(r"[A-Za-z0-9 ._-]{1,40}")
_PHONE_ID = re.compile(r"ph_[0-9a-f]{12}")


@dataclass(frozen=True)
class Phone:
    id: str
    label: str
    addon: str
    key: bytes
    paired_at: str
    revoked_at: str | None

    def __repr__(self) -> str:  # never print the key
        return f"Phone(id={self.id!r}, label={self.label!r}, addon={self.addon!r}, revoked_at={self.revoked_at!r})"


def path() -> Path:
    from orch.dashboard.launch import config_dir
    return config_dir() / "remote-humans.json"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def check_code(key: bytes) -> str:
    return f"{int.from_bytes(hashlib.sha256(b'orch/pair/v1|' + key).digest()[:4], 'big') % 1_000_000:06d}"


def pair_link(prefix: str, phone_id: str, key: bytes) -> str:
    return f"{prefix}.{phone_id}.{b64u(key)}"


def _entry(data: dict, root) -> dict:
    ws = data.setdefault("workspaces", {})
    if not isinstance(ws, dict):
        ws = data["workspaces"] = {}
    entry = ws.setdefault(workspace_key(root), {})
    if not isinstance(entry, dict):
        entry = ws[workspace_key(root)] = {}
    if not isinstance(entry.get("phones"), dict):
        entry["phones"] = {}
    return entry


def _write(mutate):
    def wrapped(data: dict):
        data["version"] = 1
        return mutate(data)
    result = update_json(path(), wrapped)
    os.chmod(path(), 0o600)  # already 0600 from mkstemp; kept so a hand-loosened file is tightened on every write
    return result


def _read(root) -> dict:
    workspaces = read_json_object(path()).get("workspaces")
    entry = workspaces.get(workspace_key(root)) if isinstance(workspaces, dict) else None
    return entry if isinstance(entry, dict) else {}


def _phone(pid: str, raw) -> Phone | None:
    try:
        key = base64.urlsafe_b64decode(raw["key"] + "=" * (-len(raw["key"]) % 4))
        if len(key) != 32 or not _PHONE_ID.fullmatch(pid):
            return None
        revoked = raw.get("revoked_at")
        return Phone(pid, str(raw["label"]), str(raw["addon"]), key, str(raw["paired_at"]),
                     str(revoked) if revoked else None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def pair(root, *, label: str, addon: str) -> tuple[Phone, str]:
    label = " ".join(str(label or "").split())
    if not _LABEL.fullmatch(label):
        raise UsageError("a phone label is 1-40 letters, digits, spaces, dots, dashes or underscores")
    key, pid, at = secrets.token_bytes(32), "ph_" + secrets.token_hex(6), stamp_s()

    def mutate(data):
        _entry(data, root)["phones"][pid] = {"label": label, "addon": addon, "key": b64u(key), "paired_at": at,
                                             "revoked_at": None}
    _write(mutate)
    return Phone(pid, label, addon, key, at, None), check_code(key)


def revoke(root, phone_id: str) -> None:
    def mutate(data):
        item = _entry(data, root)["phones"].get(phone_id)
        if isinstance(item, dict) and not item.get("revoked_at"):
            item["revoked_at"] = stamp_s()
    _write(mutate)


def phones(root) -> list[Phone]:
    raw = _read(root).get("phones")
    if not isinstance(raw, dict):
        return []
    return [p for pid, item in raw.items() if (p := _phone(pid, item)) is not None]


def find(root, phone_id: str) -> Phone | None:
    return next((p for p in phones(root) if p.id == phone_id), None)


def permissions(root) -> dict[str, bool]:
    saved = _read(root).get("permissions")
    out = dict(DEFAULT_PERMISSIONS)
    if isinstance(saved, dict):
        if saved.get("v") != PERMISSIONS_V and {k: saved.get(k) for k in _OLD_DEFAULTS} == _OLD_DEFAULTS:
            return out
        out.update({k: saved[k] is True for k in KINDS if k in saved})
        if "ticket_request" not in saved:  # saved before the kind existed: it follows the owner's approve choice
            out["ticket_request"] = out["approve"]
    return out


def set_permissions(root, values: dict) -> None:
    def mutate(data):
        _entry(data, root)["permissions"] = {"v": PERMISSIONS_V, **{k: values.get(k) is True for k in KINDS}}
    _write(mutate)
