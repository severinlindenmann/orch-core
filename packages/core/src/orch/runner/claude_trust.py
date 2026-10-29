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

**Consent.** The owner agreed when creating the workspace: ``orch setup`` / setup writes the marker
``<root>/.orch/claude-trust`` (:func:`record_consent`). Without it (an older workspace, or a folder that merely
looks like one) nothing is written. The same marker is also written later, by the dashboard's sign card for a
Claude Code start (``POST /api/w/{ws}/claude-trust``, owner on a local session only, ``claude_folder.grant_consent``);
the guards below are the same whichever way the consent arrived.

**Only for isolated launches.** The runner calls this only for a launch that carries ``--setting-sources user
--strict-mcp-config`` (a ticket work folder, see :func:`orch.host.launch.launch_argv`); worktree, repo and root launches
get no entry and keep Claude Code's own prompts. When the run exits the runner calls :func:`refresh`, which can
only take trust back. Entries of work folders that no longer exist are swept at the next :func:`pretrust`; the host
never removes them (it does not touch a person's home).

**Guards (fail closed).** The root must hold a real ``.orch`` store folder with the marker, must not equal or
contain the home folder or the Claude config folder, and must be at least two levels deep. The work folder is resolved
(links followed) and must lie under the resolved root's ``.orch-work``; if it resolves anywhere else both spellings are
rejected. It must also be the folder the spawn pinned (``dev``/``ino``). A missing, unparseable, linked or oddly
shaped config file is left alone (the prompts then appear, as before); an error here never stops a launch.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from orch.layout import STORE_DIR

__all__ = ["FOREIGN", "MARKER", "TRUST_KEYS", "pretrust", "record_consent", "refresh", "trust_paths"]

TRUST_KEYS = (
    "hasTrustDialogAccepted",
    "hasClaudeMdExternalIncludesApproved",
    "hasClaudeMdExternalIncludesWarningShown",
)
WORK_DIR = ".orch-work"
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


#: what Claude Code reads from the folder it trusts (settings with hooks, permissions and env; MCP; memory files).
#: Orch writes none of these into a ticket work folder, so any of them there came from somebody else.
FOREIGN = (".claude", ".mcp.json", "CLAUDE.md", "CLAUDE.local.md")
_IMPORT = re.compile(r"(?:^|\s)@[~/.\w]")


def _holds_foreign_config(folder: str) -> bool:
    return any(os.path.lexists(os.path.join(folder, name)) for name in FOREIGN)


def _git_tracks_work_files(root: str) -> bool:
    """True if ``git ls-files`` lists anything below ``.orch-work/`` (or git cannot say). Not a repository: False.
    Hooks, fsmonitor and the system config are off; the environment is minimal."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    try:
        r = subprocess.run(  # noqa: S603
            ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "ls-files", "-z", "--", WORK_DIR],  # noqa: S607
            cwd=root, env=env, capture_output=True, timeout=5, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return True
    if r.returncode == 0:
        return bool(r.stdout.strip(b"\0"))
    return b"not a git repository" not in r.stderr


def _imports_in(path: str) -> bool | None:
    """True if the file has an ``@path`` import line, None if it is a link or unreadable, False if clean or absent."""
    try:
        if not os.path.lexists(path):
            return False
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return None
        with open(path, encoding="utf-8") as f:
            return any(_IMPORT.search(line) for line in f.read(LIMIT).splitlines())
    except (OSError, ValueError):
        return None


def _includes_safe(root: str) -> bool:
    """The include approval is safe only if the root's ``CLAUDE.md`` is exactly what orch writes (``@AGENTS.md``) and
    every other file Claude Code could load as memory there has no import (``@path``) line: ``AGENTS.md``, the store's
    ``AGENTS.orch.md``, ``CLAUDE.local.md``, ``.claude/CLAUDE.md``, ``.claude/rules/**/*.md`` and the
    ``.orch-work/CLAUDE.md`` above the ticket folders (each may be missing)."""
    from orch.instructions.harness import AGENTS_FILE, _claude_md_text

    try:
        with open(os.path.join(root, "CLAUDE.md"), "rb") as f:
            if f.read(LIMIT) != (_claude_md_text("", True, True) or "").encode():
                return False
        if stat.S_ISLNK(os.lstat(os.path.join(root, "CLAUDE.md")).st_mode):
            return False
    except (OSError, ValueError):
        return False
    files = [
        "AGENTS.md",
        os.path.join(STORE_DIR, AGENTS_FILE),
        "CLAUDE.local.md",
        os.path.join(".claude", "CLAUDE.md"),
        os.path.join(WORK_DIR, "CLAUDE.md"),
    ]
    rules = os.path.join(root, ".claude", "rules")
    if os.path.lexists(rules):
        if os.path.islink(rules):
            return False
        for here, dirs, names in os.walk(rules):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(here, d))]
            files += [os.path.relpath(os.path.join(here, n), root) for n in names if n.endswith(".md")]
            if len(files) > 500:
                return False
    return all(_imports_in(os.path.join(root, rel)) is False for rel in files)


def _config_path(env: Mapping[str, str]) -> Path | None:
    base = env.get("CLAUDE_CONFIG_DIR") or env.get("HOME") or ""
    if not base.startswith("/"):
        return None
    return Path(base) / ".claude.json"


def pretrust(
    env: Mapping[str, str], root: str | None, cwd: str | None, pin: Mapping[str, object] | None = None
) -> list[str]:
    """Record the trust for a ``claude`` run in ``cwd`` of the workspace ``root``; returns the paths written (empty
    when nothing was needed or allowed). Never raises. A work folder that holds settings, MCP or memory files of
    somebody else, or that git tracks, gets no trust (and loses an earlier one); the include approval is added only
    when the root's instruction files are orch's own, and withdrawn when they are not. Entries for work folders of this
    root that no longer exist are swept away (the host never touches a person's config; nothing else removes them)."""
    try:
        if not root or not cwd or not _consented(root) or not _root_ok(root, env):
            return []
        paths = trust_paths(root, cwd, pin)
        cfg = _config_path(env)
        if not paths or cfg is None:
            return []
        sweep = _bases(root)
        if _holds_foreign_config(paths[0]) or _git_tracks_work_files(root):
            _write(cfg, paths, remove=True, sweep=sweep)
            return []
        return _write(cfg, paths, includes=_includes_safe(root), sweep=sweep)
    except Exception:  # noqa: BLE001 - a launch never fails over a convenience
        return []


def _bases(root: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(os.path.join(os.path.normpath(r), WORK_DIR) for r in (root, os.path.realpath(root))))


def refresh(env: Mapping[str, str], root: str, cwd: str) -> None:
    """When a run exits: check the folder again and only take trust back, never give any. Foreign config or a
    tracked file now: the entry goes; else an include approval goes if the instruction files no longer qualify.
    Never raises."""
    try:
        cfg = _config_path(env)
        if cfg is None or not _consented(root) or not _root_ok(root, env):
            return
        paths = trust_paths(root, cwd)
        if not paths:
            return
        sweep = _bases(root)
        if _holds_foreign_config(paths[0]) or _git_tracks_work_files(root):
            _write(cfg, paths, remove=True, sweep=sweep)
        elif not _includes_safe(root):
            _write(cfg, paths, includes=False, add=False, sweep=sweep)
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def _locked(cfg: Path):
    """Claude Code guards ``.claude.json`` with a ``<file>.lock`` directory (``mkdir``: exclusive, as the npm
    ``proper-lockfile`` does); take the same one. Waits up to 3 s, never steals a lock, and yields False if it could
    not get it."""
    lock = str(cfg) + ".lock"
    got = False
    deadline = time.monotonic() + 3
    while True:
        try:
            os.mkdir(lock, 0o700)
            got = True
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
        except OSError:
            break
    try:
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                os.rmdir(lock)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(
    cfg: Path,
    paths: list[str],
    *,
    includes: bool = False,
    remove: bool = False,
    add: bool = True,
    sweep: tuple[str, ...] = (),
) -> list[str]:
    """Merge the entries of ``paths`` into ``cfg`` under the lock, from a re-read; only our entries change.

    * default: set the trust key, and the two include keys when ``includes`` (else take any include key away);
    * ``add=False``: only take include keys away (never give trust);
    * ``remove``: drop the three keys;
    * ``sweep``: folders whose children are ours: an entry of a child that no longer exists loses its keys.

    The file is written beside, fsynced, and replaced only if it is still the file that was read; the directory is
    fsynced after. Anything unexpected: give up quietly (the prompts then appear)."""
    with _locked(cfg) as ok:
        if not ok:
            return []
        try:
            st = os.lstat(cfg)
        except FileNotFoundError:
            if remove or not add:
                return []
            data: dict = {}
            mode, before = 0o600, None
        else:
            if not stat.S_ISREG(st.st_mode) or st.st_size > LIMIT or st.st_uid != os.getuid():
                return []  # a link, a FIFO, a huge or foreign file: leave it alone
            mode, before = stat.S_IMODE(st.st_mode), st
            try:
                data = json.loads(cfg.read_text("utf-8"))
            except (ValueError, OSError):
                return []
            if not isinstance(data, dict):
                return []
        projects = data.get("projects")
        if projects is None:
            if remove or not add:
                return []
            projects = data["projects"] = {}
        if not isinstance(projects, dict):
            return []
        changed: list[str] = []

        def drop(key: str, names: tuple[str, ...]) -> bool:
            entry = projects.get(key)
            if not isinstance(entry, dict) or not any(k in entry for k in names):
                return False
            for k in names:
                entry.pop(k, None)
            if not entry:
                del projects[key]
            return True

        for key in list(projects):
            if sweep and os.path.dirname(key) in sweep and not os.path.lexists(key) and drop(key, TRUST_KEYS):
                changed.append(key)
        for p in paths:
            if remove:
                if drop(p, TRUST_KEYS):
                    changed.append(p)
                continue
            if not includes and drop(p, TRUST_KEYS[1:]):
                changed.append(p)
            if not add:
                continue
            entry = projects.setdefault(p, {})
            if not isinstance(entry, dict):
                continue
            keys = TRUST_KEYS if includes else TRUST_KEYS[:1]
            if any(entry.get(k) is not True for k in keys):
                entry.update({k: True for k in keys})
                if p not in changed:
                    changed.append(p)
        if not changed:
            return []
        fd, tmp = tempfile.mkstemp(prefix=".claude-", suffix=".tmp", dir=cfg.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            try:
                now = os.lstat(cfg)
            except FileNotFoundError:
                now = None
            if (now is None) != (before is None) or (
                now is not None
                and before is not None
                and (now.st_ino, now.st_mtime_ns, now.st_size) != (before.st_ino, before.st_mtime_ns, before.st_size)
            ):
                raise _Changed  # Claude Code wrote it meanwhile: do not overwrite
            if before is None:
                # the file was missing: a truly exclusive create. link() fails if Claude Code made it meanwhile,
                # even without taking its lock (a replace would overwrite it)
                try:
                    os.link(tmp, cfg)
                except FileExistsError:
                    raise _Changed from None
                os.unlink(tmp)
            else:
                os.replace(tmp, cfg)
            _fsync_dir(cfg.parent)
        except _Changed:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return []
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return changed


class _Changed(Exception):
    pass
