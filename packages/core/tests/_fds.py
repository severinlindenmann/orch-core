"""Count only the descriptors the code under test opened, never the process's whole fd table.

A raw ``len(os.listdir("/dev/fd"))`` also counts whatever else the process opens meanwhile (xdist's channel, other
tests' sockets and log files, a late finalizer), which made the leak tests flaky under load. A leak of the code under
test is a descriptor that names one of *its own* files, so those are the only ones counted: each open descriptor is
identified by ``(st_dev, st_ino)`` and kept when that identity belongs to the tree the test built (a ``tmp_path``
unique to the test)."""

from __future__ import annotations

import os
from pathlib import Path


def tree_identities(*roots: Path) -> set[tuple[int, int]]:
    """The identity of every file and directory under ``roots`` (symlinks are not followed), roots included."""
    ids: set[tuple[int, int]] = set()
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            for name in (".", *dirnames, *filenames):
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                ids.add((st.st_dev, st.st_ino))
    return ids


def open_fds_on(ids: set[tuple[int, int]]) -> int:
    """How many open descriptors of this process name one of ``ids``."""
    n = 0
    for entry in os.listdir("/dev/fd"):
        try:
            st = os.fstat(int(entry))
        except OSError:  # the listing's own descriptor, or one closed meanwhile
            continue
        if (st.st_dev, st.st_ino) in ids:
            n += 1
    return n
