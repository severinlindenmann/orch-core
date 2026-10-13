"""The fixed exec trampoline of the runner (D-P2-29): ``python -I <this file> SPEC_FD STATUS_FD``.

The runner starts every run through this file, with the PTY slave as fd 0-2. It reads one JSON spec from ``SPEC_FD``
and then, in this order:

1. resets ``SIGPIPE`` and ``SIGXFSZ`` to their defaults (Python ignores them, and an ignored signal survives exec);
2. Linux with a run cgroup: writes its own PID to ``<cgroup>/cgroup.procs``, so the run is inside its cgroup
   **before** anything of the run executes (every later descendant is born inside it);
3. ``setsid()`` and ``TIOCSCTTY`` on fd 0: the run is its own session, with the PTY as controlling terminal;
4. rlimits: ``RLIMIT_CORE`` 0; the ``RLIMIT_NOFILE`` soft limit raised to ``min(hard, 8192)``; nothing else (agents
   compile and test; no CPU or memory caps in phase 2, tie-break D-P2-29);
5. opens the pinned cwd one component at a time from ``/`` with ``O_DIRECTORY|O_NOFOLLOW``, compares the final
   directory's ``(st_dev, st_ino)`` with the pin and keeps that fd until ``fchdir`` (a swapped or symlinked path
   fails with step ``cwd``);
6. closes every fd but 0-2 (the spec pipe is closed right after reading; the status pipe is close-on-exec);
7. ``execve(argv[0], argv, env)``: ``argv[0]`` is absolute and the environment is exactly the spec's.

On failure it writes ``{"step":…, "errno":…}`` to ``STATUS_FD`` and exits 127; a successful ``execve`` closes the
status pipe, which the runner reads as success. This file imports the standard library only, and runs with ``-I``
(which implies ``-P``): neither ``PYTHONPATH``, the user site nor the script's directory reach its imports.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import resource
import signal
import sys
import termios

SPEC_LIMIT = 1024 * 1024
NOFILE = 8192


class Step(Exception):
    def __init__(self, step: str, code: int = 0) -> None:
        super().__init__(step)
        self.step, self.code = step, code


def _read_spec(fd: int) -> dict:
    chunks: list[bytes] = []
    total = 0
    while True:
        part = os.read(fd, 65536)
        if not part:
            break
        total += len(part)
        if total > SPEC_LIMIT:
            raise Step("spec")
        chunks.append(part)
    os.close(fd)
    try:
        spec = json.loads(b"".join(chunks))
    except ValueError:
        raise Step("spec") from None
    if not isinstance(spec, dict):
        raise Step("spec")
    argv, env, cwd = spec.get("argv"), spec.get("env"), spec.get("cwd")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) and "\0" not in a for a in argv)
        or not argv[0].startswith("/")
        or not isinstance(env, dict)
        or not all(isinstance(k, str) and isinstance(v, str) and "=" not in k and k for k, v in env.items())
        or not isinstance(cwd, dict)
        or not isinstance(cwd.get("path"), str)
        or not cwd["path"].startswith("/")
        or not isinstance(cwd.get("dev"), int)
        or not isinstance(cwd.get("ino"), int)
    ):
        raise Step("spec")
    return spec


def _join_cgroup(path: str) -> None:
    try:
        fd = os.open(os.path.join(path, "cgroup.procs"), os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
        finally:
            os.close(fd)
    except OSError as exc:
        raise Step("cgroup", exc.errno or 0) from None


def _open_pinned(path: str, dev: int, ino: int) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open("/", flags)
        for part in [p for p in path.split("/") if p]:
            if part in (".", ".."):
                os.close(fd)
                raise Step("cwd", errno.EINVAL)
            try:
                nxt = os.open(part, flags, dir_fd=fd)
            finally:
                os.close(fd)
            fd = nxt
    except OSError as exc:
        raise Step("cwd", exc.errno or 0) from None
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) != (dev, ino):
        os.close(fd)
        raise Step("cwd", errno.ESTALE)
    return fd


def _close_others(keep: set[int]) -> None:
    listing = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
    try:
        fds = [int(n) for n in os.listdir(listing) if n.isdigit()]
    except OSError:
        fds = list(range(3, NOFILE))
    for fd in fds:
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def run(spec_fd: int, status_fd: int) -> None:
    os.set_inheritable(status_fd, False)
    spec = _read_spec(spec_fd)
    for name in ("SIGPIPE", "SIGXFSZ"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_DFL)
    if spec.get("cgroup"):
        _join_cgroup(str(spec["cgroup"]))
    try:
        os.setsid()
    except OSError as exc:
        raise Step("setsid", exc.errno or 0) from None
    if spec.get("tty", True):
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except OSError as exc:
            raise Step("tty", exc.errno or 0) from None
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = NOFILE if hard == resource.RLIM_INFINITY else min(hard, NOFILE)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (OSError, ValueError) as exc:
        raise Step("rlimit", getattr(exc, "errno", 0) or 0) from None
    cwd = spec["cwd"]
    dirfd = _open_pinned(cwd["path"], cwd["dev"], cwd["ino"])
    try:
        os.fchdir(dirfd)
    except OSError as exc:
        raise Step("cwd", exc.errno or 0) from None
    finally:
        os.close(dirfd)
    _close_others({status_fd})
    argv = spec["argv"]
    try:
        os.execve(argv[0], argv, dict(spec["env"]))
    except OSError as exc:
        raise Step("exec", exc.errno or 0) from None


def main(argv: list[str]) -> int:
    try:
        spec_fd, status_fd = int(argv[0]), int(argv[1])
    except (IndexError, ValueError):
        return 127
    try:
        run(spec_fd, status_fd)
    except Step as exc:
        try:
            os.write(status_fd, json.dumps({"step": exc.step, "errno": exc.code}).encode())
        except OSError:
            pass
    except BaseException:  # noqa: BLE001 - anything else is a failed start, never a traceback on the PTY
        try:
            os.write(status_fd, b'{"step":"internal","errno":0}')
        except OSError:
            pass
    return 127


if __name__ == "__main__":
    os._exit(main(sys.argv[1:]))
