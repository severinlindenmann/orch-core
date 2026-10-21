"""Target normalization and owner classification from signed Landing settings."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_OWNER_TARGETS = ("main", "master")


class QueueError(ValueError):
    """A refused pure queue operation, with a stable public error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def norm_target(raw: str, remote_names: Sequence[str] = ()) -> str | None:
    """Port of the UI's ASCII-only, repeatedly prefix-stripping normTarget."""
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", raw):
        return None
    remotes = {"origin", "upstream", *(name.lower() for name in remote_names)}
    target = raw.lower()
    while True:
        before = target
        if target.startswith("refs/heads/"):
            target = target[11:]
        elif target.startswith("heads/"):
            target = target[6:]
        elif re.match(r"^(refs/)?remotes/[^/]+/", target):
            target = re.sub(r"^(refs/)?remotes/[^/]+/", "", target)
        elif target.split("/")[0] in remotes and "/" in target:
            target = target.split("/", 1)[1]
        if before == target:
            break
    if (
        not re.fullmatch(r"[a-z0-9_./-]{1,80}", target)
        or target == "head"
        or target.startswith(("-", "/"))
        or target.endswith(("/", "."))
        or re.match(r"^(refs|heads|remotes|tags)(/|$)", target)
        or target.split("/")[0] in remotes
        or "//" in target
        or ".." in target
        or any(part.startswith(".") or part.endswith(".lock") for part in target.split("/"))
    ):
        return None
    return target


def is_owner_target(settings: Mapping[str, Any], target: str) -> bool:
    """Derive classification only from owner_targets; invalid targets fail closed."""
    normalized = norm_target(target)
    if normalized is None:
        return True
    configured = settings.get("owner_targets")
    extra = configured if isinstance(configured, list) and all(isinstance(name, str) for name in configured) else []
    # main/master are owner targets always: a signed empty or partial list can add, never remove.
    owners = (*DEFAULT_OWNER_TARGETS, *extra)
    return any(norm_target(name) == normalized for name in owners)


def check_owner_flag(settings: Mapping[str, Any], target: str, claimed: bool) -> None:
    """Refuse an event flag inconsistent with the signed policy (I10)."""
    if claimed is not is_owner_target(settings, target):
        raise QueueError("land.owner_required", "owner_target differs from signed settings")
