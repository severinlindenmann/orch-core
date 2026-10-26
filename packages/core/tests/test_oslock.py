"""The lock layer (``orch.oslock``) and import-ability of every module without the POSIX-only stdlib (Windows port)."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from orch import oslock


def _open(path):
    return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)


def test_exclusive_blocks_exclusive_until_unlock(tmp_path):
    p = tmp_path / "l"
    a, b = _open(p), _open(p)
    try:
        oslock.flock(a, oslock.LOCK_EX | oslock.LOCK_NB)
        with pytest.raises(BlockingIOError):
            oslock.flock(b, oslock.LOCK_EX | oslock.LOCK_NB)
        oslock.flock(a, oslock.LOCK_UN)
        oslock.flock(b, oslock.LOCK_EX | oslock.LOCK_NB)
        oslock.flock(b, oslock.LOCK_UN)
    finally:
        os.close(a)
        os.close(b)


def test_shared_locks_coexist_but_block_exclusive(tmp_path):
    p = tmp_path / "l"
    a, b, c = _open(p), _open(p), _open(p)
    try:
        oslock.flock(a, oslock.LOCK_SH | oslock.LOCK_NB)
        oslock.flock(b, oslock.LOCK_SH | oslock.LOCK_NB)
        with pytest.raises(BlockingIOError):
            oslock.flock(c, oslock.LOCK_EX | oslock.LOCK_NB)
    finally:
        for fd in (a, b, c):
            os.close(fd)


_NO_POSIX = textwrap.dedent(
    """
    import importlib, pkgutil, sys
    for name in ("pwd", "grp", "termios", "resource", "tty"):
        sys.modules[name] = None  # import raises ImportError, as on Windows
    sys.modules["fcntl"] = None  # (orch.oslock picks its Windows branch by os.name, which stays "posix" here)
    import types
    sys.modules["fcntl"] = types.SimpleNamespace(LOCK_SH=1, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=None)
    import os, signal, socketserver
    for a in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK", "O_NOCTTY", "O_CLOEXEC", "O_SYMLINK", "killpg", "setsid",
              "getpgid", "getuid", "geteuid", "getgid", "getegid", "fork", "chown", "mkfifo", "getpgrp", "setpgid",
              "openpty", "fchmod", "fchown", "lchown", "getgroups", "initgroups", "setuid", "setgid", "setgroups"):
        if hasattr(os, a):
            delattr(os, a)
    for a in ("SIGKILL", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGALRM", "SIGCHLD", "SIGQUIT", "SIGWINCH", "SIGPIPE",
              "SIGSTOP", "SIGCONT"):
        if hasattr(signal, a):
            delattr(signal, a)
    for a in ("UnixStreamServer", "ThreadingUnixStreamServer", "UnixDatagramServer", "ForkingMixIn"):
        if hasattr(socketserver, a):
            delattr(socketserver, a)
    import orch  # installs the missing O_* flags as 0 (orch._winflags)
    bad = []
    for m in pkgutil.walk_packages(orch.__path__, "orch.", onerror=lambda n: bad.append(n + ": package import failed")):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except BaseException as e:  # noqa: BLE001
            bad.append(f"{m.name}: {type(e).__name__}: {e}")
    print("\\n".join(bad))
    sys.exit(1 if bad else 0)
    """
)


@pytest.mark.windows_smoke
def test_every_module_imports_without_posix_only_modules():
    """Simulates Windows on any OS (the POSIX-only modules are blocked); on real Windows they are simply absent."""
    r = subprocess.run([sys.executable, "-c", _NO_POSIX], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
