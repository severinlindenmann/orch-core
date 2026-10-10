"""Every path the store builds from data (uid, log name, artifact name, manifest target) goes through here.

Names are checked against the exact F1 patterns before use, joined under the workspace root, and every existing
component of the result must be a real directory or file inside the root: a symlink anywhere on the way is refused
(``validation.path``), as are ``..``, absolute paths, NUL and separators (they cannot match the patterns).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import StoreError

__all__ = ["ARTIFACT", "ULID", "check_artifact", "check_uid", "safe_join", "target_ok"]

ULID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TARGET = re.compile(
    r"(?:config\.json|keys\.jsonl|tickets/[0-7][0-9A-HJKMNP-TV-Z]{25}/(?:ticket\.json|body\.md"
    r"|artifacts/[A-Za-z0-9][A-Za-z0-9._-]{0,127}))"
)


def check_uid(uid: object) -> str:
    if type(uid) is not str or not ULID.fullmatch(uid):
        raise StoreError("validation.path", f"{uid!r} is not a ticket uid")
    return uid


def check_artifact(name: object) -> str:
    if type(name) is not str or not ARTIFACT.fullmatch(name):
        raise StoreError("validation.path", f"{name!r} is not an artifact name")
    return name


def target_ok(rel: object) -> str:
    """A projection target a pending manifest may name: nothing but the files the store writes."""
    if type(rel) is not str or not _TARGET.fullmatch(rel):
        raise StoreError("validation.path", f"{rel!r} is not a file the store writes")
    return rel


def safe_join(root: Path, rel: str) -> Path:
    """``root/rel`` with no symlinked directory on the way (the last part may be absent or a link), no escape."""
    if "\x00" in rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or "\\" in rel:
        raise StoreError("validation.path", f"{rel!r} escapes the workspace")
    cur = root
    parts = Path(rel).parts
    for part in parts[:-1]:  # the last component may be a link: replacing it replaces the link, never its target
        cur = cur / part
        if os.path.islink(cur):
            raise StoreError("validation.path", f"{rel!r} passes through a symlink")
    base = os.path.realpath(root)
    if os.path.commonpath([base, os.path.realpath(cur)]) != base:
        raise StoreError("validation.path", f"{rel!r} escapes the workspace")
    return root / rel
