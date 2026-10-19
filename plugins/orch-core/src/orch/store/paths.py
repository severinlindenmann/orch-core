"""Every path the store builds from data (uid, log name, artifact name, manifest target) goes through here.

Names are checked against the exact F1 patterns before use, joined under the workspace root, and every existing
component of the result must be a real directory or file inside the root: a symlink anywhere on the way is refused
(``validation.path``), as are ``..``, absolute paths, NUL and separators (they cannot match the patterns).
"""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import StoreError

if TYPE_CHECKING:
    from .rootfs import RootFS

__all__ = ["ARTIFACT", "ULID", "check_artifact", "check_state_dirs", "check_uid", "safe_join", "target_ok"]

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
    """``root/rel``, checked **lexically** only (no ``..``, absolute path, NUL or backslash): no file system access.
    Every file under the root is reached through the store's pinned root (:class:`~orch.store.rootfs.RootFS`), which
    walks each component ``O_NOFOLLOW`` and refuses a symlinked directory (``validation.path``) at the access itself;
    a check here by pathname would only be a second, racy look at a different object (#564)."""
    if "\x00" in rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or "\\" in rel:
        raise StoreError("validation.path", f"{rel!r} escapes the workspace")
    return root / rel


_DIRS = ("events", "tickets", ".state", ".state/pending", ".state/body", ".state/checkpoints", ".state/intents",
         ".state/sessions", ".state/abandoned", ".state/rejected")  # fmt: skip


def check_state_dirs(fs: RootFS) -> None:
    """The directories the store writes under must be real directories of the workspace: a symlink (``.state`` pointing
    elsewhere, say) would send writes outside it (``validation.path``). Checked below the pinned root (#516); every
    later access walks ``O_NOFOLLOW`` again, so this is the early, named refusal, not the boundary."""
    for rel in _DIRS:
        try:
            st = fs.lstat(rel)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISLNK(st.st_mode):
            raise StoreError("validation.path", f"{rel} is a symlink")
        if not stat.S_ISDIR(st.st_mode):
            raise StoreError("validation.path", f"{rel} is not a directory")
