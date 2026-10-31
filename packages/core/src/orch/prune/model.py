"""``orch host prune``: the data model, the seams and the safety fences shared by the inventory and the removal.

Everything that touches the machine is a seam on :class:`Ctx` (the tests replace them): running a program, asking the
person, the install runner, the keychain, the OS user database. Nothing in this package deletes a path that was not
first recorded as an :class:`Item` by :mod:`orch.prune.inventory`, and :func:`Fence.check` is the one place that decides
whether a path may be removed: it lies below a root orch records or owns, no component below that root is a link, and
it is neither the home folder, a parent of it, nor shallow.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from orch import childenv

from .safe import real_keychain, safe_run, tty_ask

__all__ = ["Ctx", "Fence", "Inventory", "Item", "PruneError", "WorkspaceInv", "human_size", "measure"]


class PruneError(Exception):
    """A refusal or a failed step, in plain words (printed as ``orch host prune: <message>``)."""


@dataclass
class Item:
    """One thing prune found on this computer.

    ``kind`` decides how it is removed: ``unit`` (a launchd/systemd unit), ``part`` (an installed hub/relay/publish/
    dashboard service), ``launcher`` (the dashboard started by hand), ``tree`` (a folder, removed without following
    links), ``file``, ``keychain`` (one item of a macOS keychain), ``claude-trust`` (orch's entries in Claude Code's
    ``.claude.json``), ``worktree`` (a git worktree of a ticket), ``edit`` (orch's lines in a file of the person's),
    ``folder`` (a whole workspace folder, only when the person said so), ``os`` (needs root), ``tool`` (orch itself).
    ``note`` items (kind ``note``) are shown only."""

    group: str
    kind: str
    label: str
    path: str = ""
    files: int = 0
    size: int = 0
    note: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        bits = [self.label]
        if self.path:
            bits.append(self.path)
        if self.files or self.size:
            bits.append(f"({self.files} files, {human_size(self.size)})")
        if self.note:
            bits.append(f"- {self.note}")
        return "  ".join(bits)


@dataclass
class WorkspaceInv:
    """A workspace folder on this computer: orch's own files in it (always removed) and the whole folder (removed only
    when the person says so)."""

    root: Path
    wid: str | None
    items: list[Item] = field(default_factory=list)
    files: int = 0
    size: int = 0
    folder_refusal: str = ""  # why the whole folder can never be deleted by prune ("" = it may be asked)
    hub: bool = False  # on a hub somewhere else (one that prune does not remove)
    local_hub: bool = False  # its hub is installed on this computer and goes with this prune


@dataclass
class Inventory:
    state: Path
    home: Path
    items: list[Item] = field(default_factory=list)
    workspaces: list[WorkspaceInv] = field(default_factory=list)
    remote: list[str] = field(default_factory=list)  # leftovers on other computers that prune cannot delete
    not_removable: list[str] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)  # every root a removal may lie below
    root_commands: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # why prune must not run now (a hand-started host)
    idents: dict[Path, tuple[int, int]] = field(default_factory=dict)  # (st_dev, st_ino) of each folder root
    rmdirs: list[Path] = field(default_factory=list)  # removed after everything else, only if empty

    def groups(self) -> dict[str, list[Item]]:
        out: dict[str, list[Item]] = {}
        for it in self.items:
            out.setdefault(it.group, []).append(it)
        return out


def _leave(root: Path, env: Mapping[str, str]) -> tuple[bool, str]:
    """Tell the hub this workspace leaves: ``orch hub leave`` in the person's own terminal (it shows its review).
    Isolated interpreter (``-I``: no current directory, no PYTHONPATH on ``sys.path``) started from a neutral folder;
    the workspace is named by ``ORCH_WORKSPACE``, so a file in the workspace (``orch.py``) is never imported."""
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    child = childenv.sanitize(env)
    child["ORCH_WORKSPACE"] = os.fspath(root)
    try:
        r = subprocess.run([sys.executable, "-I", "-m", "orch", "hub", "leave"], cwd=home, env=child, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, f"exit {r.returncode}"


def _find_key(doc: Any, key: str) -> Any:
    if isinstance(doc, dict):
        if key in doc:
            return doc[key]
        for v in doc.values():
            got = _find_key(v, key)
            if got is not None:
                return got
    return None


def _hub_joined(root: Path, env: Mapping[str, str]) -> bool | None:
    """Whether the workspace is joined to a hub RIGHT NOW (``orch hub status --json`` asks the live host, not the
    leftover files): ``True``/``False``, ``None`` when that could not be read. Same isolation as :func:`_leave`."""
    import json

    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    child = childenv.sanitize(env)
    child["ORCH_WORKSPACE"] = os.fspath(root)
    try:
        r = subprocess.run(
            [sys.executable, "-I", "-m", "orch", "hub", "status", "--json"],
            cwd=home, env=child, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    text = r.stdout or ""
    start = text.find("{")
    try:
        doc = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        doc = None
    if "hub.not_joined" in text or "no hub" in text:
        return False
    if doc is None:
        return None
    if not _find_key(doc, "hub_id"):
        return False
    return bool(_find_key(doc, "joined"))


@dataclass
class Ctx:
    env: Mapping[str, str]
    out: TextIO
    err: TextIO
    interactive: bool = False
    platform: str = sys.platform
    #: test seam for running programs: ``(argv, env | None) -> (exit code, output)``; ``None``: :func:`safe_run`
    run: Callable[..., tuple[int, str]] | None = None
    ask: Callable[[str], str] = tty_ask
    #: folders prune may touch (set by the inventory): no program is ever taken from below them
    deny: list[Path] = field(default_factory=list)
    hub_leave: Callable[[Path, Mapping[str, str]], tuple[bool, str]] = _leave
    #: ``(workspace root, env) -> joined now? | None (unknown)``
    hub_status: Callable[[Path, Mapping[str, str]], bool | None] = _hub_joined
    #: install runner (launchd / systemd / pidfile) of the installed parts; ``None``: pick it from the layout
    runner: Any = None
    #: ``[(name, kind)]`` of OS users/groups orch created that exist here (``kind`` user|group); Linux only
    os_accounts: Callable[[], list[tuple[str, str]]] | None = None
    #: macOS keychain: ``(path | None) -> (accounts, delete(account))`` or ``None`` when unavailable
    keychain: Callable[[str | None], Any] | None = None
    dashboard_entry: Callable[[], list[str] | None] | None = None
    euid: int = field(default_factory=lambda: os.geteuid() if hasattr(os, "geteuid") else 1)
    uid: int = field(default_factory=lambda: os.getuid() if hasattr(os, "getuid") else 1)
    root_exists: Callable[[str], bool] = lambda p: os.path.lexists(p)
    #: hub removal order (:mod:`orch.prune.hub_order`) seams: wall clock in seconds, sleep, and the path of the orch
    #: program that runs this prune (``None``: ``sys.argv[0]`` and the interpreter)
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    orch_exe: str | None = None

    def __post_init__(self) -> None:
        if self.keychain is None and self.platform == "darwin":
            self.keychain = real_keychain

    def exec(self, argv: Sequence[str], env: Mapping[str, str] | None = None) -> tuple[int, str]:
        """Run a program: through the test seam, or by absolute path, scrubbed environment, from the home folder."""
        if self.run is not None:
            return self.run(list(argv), env)
        return safe_run(argv, {**self.env, **(env or {})}, self.deny)

    @property
    def home(self) -> Path:
        h = self.env.get("HOME") or self.env.get("USERPROFILE")
        return Path(h) if h else Path.home()

    def say(self, line: str = "") -> None:
        self.out.write(line + "\n")
        self.out.flush()


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def measure(path: str | os.PathLike[str]) -> tuple[int, int]:
    """``(files, bytes)`` below ``path`` without following a link (a link counts as one file of its own size)."""
    files = size = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return 1, st.st_size
    stack = [os.fspath(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(s.st_mode):
                        stack.append(e.path)
                    else:
                        files += 1
                        size += s.st_size
        except OSError:
            continue
    return files, size


class Fence:
    """The one decision whether a path may be removed. ``roots``: folders and exact files orch owns. ``idents``: the
    ``(st_dev, st_ino)`` of each folder root as the inventory saw it; a root that is now a link or another folder is
    refused (a swap after the inventory). Prototype level: the check and the operation still go by name."""

    def __init__(self, roots: Sequence[Path], home: Path, idents: Mapping[Path, tuple[int, int]] | None = None) -> None:
        self.roots = [Path(os.path.abspath(r)) for r in roots]
        self.home = Path(os.path.abspath(home))
        self.idents = {Path(os.path.abspath(k)): v for k, v in (idents or {}).items()}

    def _verify_root(self, root: Path) -> None:
        want = self.idents.get(root)
        if want is None:
            return
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode) or (st.st_dev, st.st_ino) != want:
            raise PruneError(f"refusing {root}: it is not the folder prune found (it is a link or was replaced)")

    def check(self, path: str | os.PathLike[str]) -> Path:
        p = Path(os.path.abspath(path))
        if len(p.parts) < 3 or p == Path(p.anchor):
            raise PruneError(f"refusing {p}: too close to the root of the disk")
        if p == self.home or p in self.home.parents:
            raise PruneError(f"refusing {p}: it is your home folder or a parent of it")
        root = next((r for r in self.roots if p == r or r in p.parents), None)
        if root is None:
            raise PruneError(f"refusing {p}: it is not a place orch records or owns")
        self._verify_root(root)
        cur = root
        for part in p.relative_to(root).parts[:-1]:  # a link on the way would lead out of the root
            cur = cur / part
            try:
                if stat.S_ISLNK(os.lstat(cur).st_mode):
                    raise PruneError(f"refusing {p}: {cur} is a link (prune never follows links)")
            except FileNotFoundError:
                break
            except OSError as e:
                raise PruneError(f"cannot look at {cur}: {e}") from None
        return p
