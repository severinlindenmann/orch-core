"""Linux run cgroups (D-P2-29): one child cgroup per run under the runner unit's delegated subtree.

The runner unit has ``Delegate=yes``; the runner finds its own cgroup v2 directory (``/proc/self/cgroup`` line
``0::<path>``) and uses it as the base only when it is writable and has ``cgroup.kill`` (Linux 5.14). Each run gets
``<base>/run-<run id>``; the trampoline writes its own PID to that directory's ``cgroup.procs`` before ``execve``
(:mod:`orch.runner.exec`), so nothing of the run ever executes outside it. Stop writes ``1`` to ``cgroup.kill`` and
checks ``cgroup.events`` for ``populated 0``. ``complete`` then means "nothing is left in the run's cgroup and the
escape scan saw nothing leave it", not "nothing of the run is left": the delegated subtree is writable by the person's
UID, so a same-UID run can move itself out of its cgroup (or start a ``systemd-run --user`` unit). The escape scan
(:mod:`orch.runner.supervise`) checks parent links for cgroup runs too and reports such a run ``incomplete``.

Without a usable base (no cgroup v2, no delegation, macOS) :func:`detect` returns ``None`` and the runner falls back to
process-group and session sweeps, a best effort (:mod:`orch.runner.supervise`).
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path

__all__ = ["CgroupTree", "detect"]

ROOT = Path("/sys/fs/cgroup")


class CgroupTree:
    def __init__(self, base: Path) -> None:
        self.base = Path(base)

    def path(self, run: str) -> Path:
        if not re.fullmatch(r"r_[0-9A-Za-z]{1,40}", run):
            raise ValueError("invalid run id for a cgroup name")
        return self.base / f"run-{run}"

    def create(self, run: str) -> Path:
        path = self.path(run)
        os.mkdir(path, 0o755)
        return path

    @staticmethod
    def _read(path: Path, name: str) -> str:
        fd = os.open(path / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            return os.read(fd, 1 << 20).decode("ascii", "replace")
        finally:
            os.close(fd)

    def procs(self, path: Path) -> list[int]:
        try:
            return [int(x) for x in self._read(path, "cgroup.procs").split()]
        except (OSError, ValueError):
            return []

    def populated(self, path: Path) -> bool | None:
        """``cgroup.events`` ``populated``; ``None`` when it cannot be read (the cgroup is gone or unreadable)."""
        try:
            text = self._read(path, "cgroup.events")
        except OSError:
            return None
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            if key == "populated":
                return value.strip() != "0"
        return None

    def kill(self, path: Path) -> bool:
        try:
            fd = os.open(path / "cgroup.kill", os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return False
        try:
            os.write(fd, b"1")
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def remove(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            os.rmdir(path)


def own_cgroup(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return None


def detect(root: Path = ROOT) -> CgroupTree | None:
    """The runner's delegated cgroup v2 directory, or ``None`` (not Linux, no v2, not writable, no ``cgroup.kill``)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        rel = own_cgroup(Path("/proc/self/cgroup").read_text())
    except OSError:
        return None
    if rel is None or rel == "/":
        return None
    base = Path(root) / rel.lstrip("/")
    if not (base / "cgroup.kill").exists():
        return None
    if not os.access(base, os.W_OK) or not os.access(base / "cgroup.procs", os.W_OK):
        return None
    return CgroupTree(base)
