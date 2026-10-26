"""Windows port, step 1: make the POSIX ``os.O_*`` open flags importable.

Windows has no ``O_NOFOLLOW``, ``O_DIRECTORY``, ``O_NONBLOCK``, ``O_NOCTTY``, ``O_CLOEXEC`` or ``O_SYMLINK``; about 120
modules read them at import time (``flags = os.O_RDONLY | os.O_NOFOLLOW``). Where one is missing it is defined as ``0``
so the modules load. That is NOT a safe-open: the symlink and directory checks those flags give are absent on Windows
until the safe-open layer (step 3 of the port) replaces them with reparse-point checks. Nothing changes on POSIX.
"""

from __future__ import annotations

import os

_FLAGS = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC", "O_SYMLINK")


def install(*, force: bool = False) -> None:
    if os.name != "nt" and not force:  # POSIX: never touch the os module (hasattr checks stay true to the platform)
        return
    for name in _FLAGS:
        if not hasattr(os, name):
            setattr(os, name, 0)
