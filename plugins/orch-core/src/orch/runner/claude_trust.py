"""Pre-trust the workspace for Claude Code, so a "Start agent" run reaches its first message without the two first-run
prompts ("Do you trust this folder?" and "Allow external CLAUDE.md file imports?", run 6, #1066).

**Where it is stored.** Claude Code keeps both answers per project path in the person's own ``~/.claude.json``
(``$CLAUDE_CONFIG_DIR/.claude.json`` when that is set for the runner): ``projects["<path>"]`` gets
``hasTrustDialogAccepted``, ``hasClaudeMdExternalIncludesApproved`` and ``hasClaudeMdExternalIncludesWarningShown``,
all ``true``. The runner writes them, as the person (it runs under the person's uid, the host never touches a person's
home), just before it starts a ``claude`` agent.

**Only workspace paths.** An entry is made for the workspace root and for the run's folder, and only when that folder
is the root itself or lies below ``<root>/.orch-work/`` (the ticket work folders). Anything else, including a git
worktree of a linked repository (outside the workspace), gets nothing, so the owner is still asked there. ``root``
comes from the host-chosen ``ORCH_WORKSPACE``. Nothing is ever written for a parent of the root or for ``/``.

**Fail soft, never fail open.** A missing, unparseable, linked or oddly shaped config file is left alone (the prompts
then appear, as before); an error here never stops a launch. Other keys and projects are kept as they are.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

__all__ = ["TRUST_KEYS", "pretrust", "trust_paths"]

TRUST_KEYS = (
    "hasTrustDialogAccepted",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
)
WORK_DIR = ".orch-work"
LIMIT = 8 << 20


def _inside(path: str, root: str) -> bool:
    p, r = Path(path), Path(root)
    return p == r or r in p.parents


def trust_paths(root: str, cwd: str) -> list[str]:
    """The project paths to trust for a run in ``cwd`` of the workspace ``root``: the root and ``cwd``, in their
    spelling and as resolved (links followed), each only if it is the root or below ``<root>/.orch-work/``. Empty when
    ``cwd`` is anywhere else."""
    if not (root.startswith("/") and cwd.startswith("/")) or "\0" in root + cwd:
        return []
    out: list[str] = []
    pairs = [(root, cwd)]
    real_root, real_cwd = os.path.realpath(root), os.path.realpath(cwd)
    if (real_root, real_cwd) != (root, cwd):
        pairs.append((real_root, real_cwd))
    for r, c in pairs:
        r, c = os.path.normpath(r), os.path.normpath(c)
        if r == "/" or not (c == r or _inside(c, os.path.join(r, WORK_DIR))):
            continue
        out += [p for p in (r, c) if p not in out]
    return out


def _config_path(env: Mapping[str, str]) -> Path | None:
    base = env.get("CLAUDE_CONFIG_DIR") or env.get("HOME") or ""
    if not base.startswith("/"):
        return None
    return Path(base) / ".claude.json"


def pretrust(env: Mapping[str, str], root: str | None, cwd: str | None) -> list[str]:
    """Record the trust for a ``claude`` run in ``cwd`` of the workspace ``root``; returns the paths written (empty
    when nothing was needed or possible). Never raises."""
    try:
        paths = trust_paths(root, cwd) if root and cwd else []
        cfg = _config_path(env)
        if not paths or cfg is None:
            return []
        return _write(cfg, paths)
    except Exception:  # noqa: BLE001 - a launch never fails over a convenience
        return []


def _write(cfg: Path, paths: list[str]) -> list[str]:
    try:
        st = os.lstat(cfg)
    except FileNotFoundError:
        data: dict = {}
        mode = 0o600
    else:
        if not stat.S_ISREG(st.st_mode) or st.st_size > LIMIT or st.st_uid != os.getuid():
            return []  # a link, a FIFO, a huge or foreign file: leave it alone
        mode = stat.S_IMODE(st.st_mode)
        try:
            data = json.loads(cfg.read_text("utf-8"))
        except (ValueError, OSError):
            return []
        if not isinstance(data, dict):
            return []
    projects = data.get("projects")
    if projects is None:
        projects = data["projects"] = {}
    if not isinstance(projects, dict):
        return []
    changed: list[str] = []
    for p in paths:
        entry = projects.get(p)
        if entry is None:
            entry = projects[p] = {}
        if not isinstance(entry, dict):
            continue
        if any(entry.get(k) is not True for k in TRUST_KEYS):
            entry.update({k: True for k in TRUST_KEYS})
            changed.append(p)
    if not changed:
        return []
    fd, tmp = tempfile.mkstemp(prefix=".claude-", suffix=".tmp", dir=cfg.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, cfg)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return changed
