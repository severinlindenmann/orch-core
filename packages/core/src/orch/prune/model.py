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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

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
    hub: bool = False


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
    rmdirs: list[Path] = field(default_factory=list)  # removed after everything else, only if empty

    def groups(self) -> dict[str, list[Item]]:
        out: dict[str, list[Item]] = {}
        for it in self.items:
            out.setdefault(it.group, []).append(it)
        return out


def _run(argv: Sequence[str], timeout: float = 60.0) -> tuple[int, str]:
    try:
        r = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _leave(root: Path, env: Mapping[str, str]) -> tuple[bool, str]:
    """Tell the hub this workspace leaves: ``orch hub leave`` in the person's own terminal (it shows its review)."""
    try:
        r = subprocess.run([sys.executable, "-m", "orch", "hub", "leave"], cwd=root, env=dict(env), timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, f"exit {r.returncode}"


@dataclass
class Ctx:
    env: Mapping[str, str]
    out: TextIO
    err: TextIO
    interactive: bool = False
    platform: str = sys.platform
    run: Callable[[Sequence[str]], tuple[int, str]] = lambda argv: _run(argv)
    ask: Callable[[str], str] = _ask
    hub_leave: Callable[[Path, Mapping[str, str]], tuple[bool, str]] = _leave
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
    """The one decision whether a path may be removed. ``roots``: folders and exact files orch owns."""

    def __init__(self, roots: Sequence[Path], home: Path) -> None:
        self.roots = [Path(os.path.abspath(r)) for r in roots]
        self.home = Path(os.path.abspath(home))

    def check(self, path: str | os.PathLike[str]) -> Path:
        p = Path(os.path.abspath(path))
        if len(p.parts) < 3 or p == Path(p.anchor):
            raise PruneError(f"refusing {p}: too close to the root of the disk")
        if p == self.home or p in self.home.parents:
            raise PruneError(f"refusing {p}: it is your home folder or a parent of it")
        root = next((r for r in self.roots if p == r or r in p.parents), None)
        if root is None:
            raise PruneError(f"refusing {p}: it is not a place orch records or owns")
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
