"""The reader in-process, for direct mode (section 3: ``git`` in direct mode is "the same reader, in-process").

No child, no audit hook, no pin store: the pins are taken afresh on every call (direct mode has no registration).
Only new reads use it (``diff``); P1's observation keeps the git CLI of :mod:`orch.store.observe` unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from . import ops
from .pinning import pin_repo
from .repo import Repo

__all__ = ["diff", "observe"]


def _roots(path: str | os.PathLike[str], roots: list[str] | None) -> tuple[str, list[str]]:
    real = os.path.realpath(os.fspath(path))
    rs = [os.path.realpath(r) for r in (roots or [])]
    return real, rs if real in rs else [real, *rs]


def observe(path: str | os.PathLike[str], refs: list[str], roots: list[str] | None = None) -> dict[str, Any]:
    real, rs = _roots(path, roots)
    with Repo(pin_repo(real, rs)) as repo:
        return ops.observe(repo, refs)


def diff(
    path: str | os.PathLike[str],
    *,
    branch: str,
    default_branch: str | None,
    context: int = 3,
    max_bytes: int = 262144,
    roots: list[str] | None = None,
) -> dict[str, Any]:
    real, rs = _roots(path, roots)
    with Repo(pin_repo(real, rs)) as repo:
        return ops.diff(repo, branch, default_branch, context, max_bytes)
