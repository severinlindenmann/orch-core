"""Pre-trust a ticket's work folder for Claude Code, so a "Start agent" run reaches its first message without the two
first-run prompts ("Do you trust this folder?" and "Allow external CLAUDE.md file imports?", run 6, #1066).

**Where it is stored.** Claude Code keeps both answers per project path in the person's own ``~/.claude.json``
(``$CLAUDE_CONFIG_DIR/.claude.json`` when that is set for the runner): ``projects["<path>"]`` gets
``hasTrustDialogAccepted``, ``hasClaudeMdExternalIncludesApproved`` and ``hasClaudeMdExternalIncludesWarningShown``,
all ``true``. The runner writes them, as the person (it runs under the person's uid; the host never touches a person's
home), just before it starts a ``claude`` agent.

**Scope.** Exactly one project entry: the ticket's work folder ``<root>/.orch-work/<KEY>`` (in its real spelling, and
its nominal one when that lies under the nominal root). Never the workspace root, never a parent, never a git worktree
of a linked repository. Claude Code's include approval is one boolean per project path: it cannot name a file. The
entry is that single folder, whose only external import is the workspace's ``AGENTS.md`` (through the root's
``CLAUDE.md``); the approval reaches no other project.

**Consent.** The owner agreed when creating the workspace: ``orch init`` / setup writes the marker
``<root>/.orch/claude-trust`` (:func:`record_consent`). Without it (an older workspace, or a folder that merely
looks like one) nothing is written.

**Guards (fail closed).** The root must hold a real ``.orch`` store folder with the marker, must not equal or
contain the home folder or the Claude config folder, and must be at least two levels deep. The work folder is resolved
(links followed) and must lie under the resolved root's ``.orch-work``; if it resolves anywhere else both spellings are
rejected. It must also be the folder the spawn pinned (``dev``/``ino``). A missing, unparseable, linked or oddly shaped
config file is left alone (the prompts then appear, as before); an error here never stops a launch.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

__all__ = ["MARKER", "TRUST_KEYS", "pretrust", "record_consent", "trust_paths"]

TRUST_KEYS = (
    "hasTrustDialogAccepted",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
)
WORK_DIR = ".orch-work"
STORE_DIR = ".orch"
#: the owner's consent, written by init inside the store folder
MARKER = "claude-trust"
LIMIT = 8 << 20


def _inside(path: str, root: str) -> bool:
    p, r = Path(path), Path(root)
    return p == r or r in p.parents


def record_consent(store: Path) -> bool:
    """Init's act: remember that the owner agreed to pre-trust this workspace's work folders. ``store`` is the
    ``.orch`` folder (a real directory); the marker is made without following a link. False if it could not be made."""
    try:
        if store.name != STORE_DIR or not stat.S_ISDIR(os.lstat(store).st_mode):
            return False
        fd = os.open(store / MARKER, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write("the owner agreed at setup: Claude Code agents start in ticket folders unasked\n")
    return True


def _consented(root: str) -> bool:
    """The root holds a real ``.orch`` folder with the marker (both without links), owned by this user."""
    try:
        st = os.lstat(os.path.join(root, STORE_DIR))
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return False
        m = os.lstat(os.path.join(root, STORE_DIR, MARKER))
        return stat.S_ISREG(m.st_mode) and m.st_uid == os.getuid()
    except OSError:
        return False


def _root_ok(root: str, env: Mapping[str, str]) -> bool:
    """Not ``/``, not a top-level folder, not the home or Claude config folder or any folder above them."""
    real = os.path.realpath(root)
    if len(Path(real).parts) < 3:  # "/" and "/Users" are 1 and 2 parts
        return False
    for var in ("HOME", "CLAUDE_CONFIG_DIR"):
        v = env.get(var)
        if v and v.startswith("/"):
            for guard in {os.path.realpath(v), os.path.normpath(v)}:
                if _inside(guard, real):  # the root equals or contains it
                    return False
    return True


def trust_paths(root: str, cwd: str, pin: Mapping[str, object] | None = None) -> list[str]:
    """The project paths to trust for a run in ``cwd`` of the workspace ``root``: the ticket's work folder
    ``<root>/.orch-work/<KEY>`` in its resolved spelling, plus its nominal one when that too lies under the nominal
    root. A folder that resolves anywhere else, the root itself, a deeper or other folder, or one that is not the
    pinned ``dev``/``ino``: empty."""
    if not (root.startswith("/") and cwd.startswith("/")) or "\0" in root + cwd:
        return []
    real_root, real_cwd = os.path.realpath(root), os.path.realpath(cwd)
    base = os.path.join(real_root, WORK_DIR)
    if os.path.dirname(os.path.normpath(real_cwd)) != base:  # exactly <root>/.orch-work/<KEY>, links resolved
        return []
    if os.path.islink(base):
        return []
    try:
        st = os.stat(real_cwd)
    except OSError:
        return []
    if pin is not None and (pin.get("dev"), pin.get("ino")) != (st.st_dev, st.st_ino):
        return []
    out = [os.path.normpath(real_cwd)]
    nominal = os.path.normpath(cwd)
    if nominal != out[0] and os.path.dirname(nominal) == os.path.join(os.path.normpath(root), WORK_DIR):
        out.append(nominal)
    return out


def _config_path(env: Mapping[str, str]) -> Path | None:
    base = env.get("CLAUDE_CONFIG_DIR") or env.get("HOME") or ""
    if not base.startswith("/"):
        return None
    return Path(base) / ".claude.json"


def pretrust(
    env: Mapping[str, str], root: str | None, cwd: str | None, pin: Mapping[str, object] | None = None
) -> list[str]:
    """Record the trust for a ``claude`` run in ``cwd`` of the workspace ``root``; returns the paths written (empty
    when nothing was needed or allowed). Never raises."""
    try:
        if not root or not cwd or not _consented(root) or not _root_ok(root, env):
            return []
        paths = trust_paths(root, cwd, pin)
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
