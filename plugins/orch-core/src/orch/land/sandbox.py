"""The OS sandbox for landing checks (design #596, invariant I7). The ONLY place that spawns check processes.

Checks run repository code from a ticket branch. Same UID, a scrubbed environment and ``HOME=<scratch>`` still leave
``~/.ssh``, the landing key file, the host state, the launchd ssh-agent socket and ``/usr/bin/security`` (the login
keychain). So a check runs only under an OS sandbox that denies everything by default:

* macOS: ``/usr/bin/sandbox-exec`` with a generated Seatbelt profile (``(deny default)``: no network, no unix sockets,
  no mach lookups (so no securityd / keychain services), reads only of the export, the toolchain and a fixed system
  list, writes only to a private temp and the output directory), plus explicit trailing denies for every path the
  caller names as forbidden (host state dir, the ``land`` dir, the key file, ``~/.ssh``).
* Linux: ``bwrap --unshare-all`` (user, pid, net, ipc, uts, cgroup namespaces), ``--die-with-parent``,
  ``--new-session``, ``--cap-drop ALL``, ``--clearenv``; only the export (read-only, ``/work``), the output dir
  (``/out``), the private temp (``/tmp``, also ``HOME``) and the toolchain (read-only, same paths) are visible.

No usable backend means :class:`NoSandbox` (``land.no_sandbox``): a check never runs unsandboxed. A backend counts as
usable only if a trivial command really runs under it (user namespaces can be switched off on a host). Executables are
found at fixed absolute paths, never through ``PATH``.

The child gets: an environment built from scratch (an allowlist, see :data:`ENV_ALLOWED`), no fds above 2, stdin from
``/dev/null``, its own session and process group. The whole group is killed at the timeout and again when the main
process exits, before the result is returned. Output is merged, capped at ``max_output`` bytes, and is data (the caller
redacts it before it reaches a log).

Known limit (macOS): ``sandbox-exec`` has no pid namespace, so a descendant that calls ``setsid`` leaves the process
group and can outlive the check. It stays inside the sandbox (no network, no secrets); the Linux backend has none of it.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from orch import fdwait

CODE = "land.no_sandbox"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
BWRAP_CANDIDATES = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap", "/opt/homebrew/bin/bwrap")
DEFAULT_MAX_OUTPUT = 64 * 1024  # design section 8: log output per check
DEFAULT_TIMEOUT = 30 * 60.0

#: environment names a caller may pass through; everything else is refused (never silently dropped).
ENV_ALLOWED = frozenset(
    {"LANG", "LC_ALL", "TZ", "CI", "NO_COLOR", "TERM", "PYTHONHASHSEED", "PYTHONDONTWRITEBYTECODE", "NODE_ENV"}
)
_SAFE_PATH = re.compile(r"^/[^\x00-\x1f\x7f\"\\]*$")  # embeddable in a Seatbelt string; no control chars
_SAFE_VALUE = re.compile(r"^[^\x00-\x1f\x7f]*$")
_SYSTEM_BIN = ("/usr/bin", "/bin")

# Fixed, read-only system locations a toolchain needs (macOS). Not /Users, /Library/Keychains, /private/var/root ...
_MAC_SYSTEM_READ = (
    "/usr", "/bin", "/sbin", "/System", "/Library/Apple", "/Library/Preferences/Logging",
    "/private/var/select", "/private/var/db/timezone", "/private/var/db/dyld",
    "/private/etc/ssl", "/private/etc/localtime",
    "/private/etc/passwd", "/private/etc/group", "/private/etc/resolv.conf", "/private/etc/hosts",
    "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper",
)  # fmt: skip
_MAC_DENY_EXEC = ("/usr/bin/security", "/usr/bin/ssh-add", "/usr/bin/ssh-agent")  # defence in depth; mach is denied too


class SandboxError(Exception):
    """A request the sandbox refuses (bad path, forbidden overlap, disallowed env name)."""


class NoSandbox(SandboxError):
    """No usable backend on this host (``land.no_sandbox``). The check does NOT run."""

    code = CODE


@dataclass(frozen=True)
class Spec:
    """One check to run. ``argv[0]`` is an absolute path to an executable inside the toolchain or system dirs."""

    argv: Sequence[str]
    export_dir: Path  # read-only, the working directory (macOS: its real path; bwrap: ``/work``)
    out_dir: Path  # writable build output (bwrap: ``/out``)
    tmp_dir: Path  # private writable scratch, ``HOME`` and ``TMPDIR`` (bwrap: ``/tmp``)
    toolchain_ro: Sequence[Path] = ()  # extra read-only (and executable) paths: interpreters, node, cargo ...
    forbidden: Sequence[
        Path
    ] = ()  # host state dir, <state>/land, the key file, ~/.ssh: no overlap allowed, denied again
    env: Mapping[str, str] = field(default_factory=dict)  # subset of ENV_ALLOWED
    timeout: float = DEFAULT_TIMEOUT
    max_output: int = DEFAULT_MAX_OUTPUT


@dataclass(frozen=True)
class Result:
    returncode: int  # negative: killed by that signal
    timed_out: bool
    output: bytes  # merged stdout+stderr, at most ``max_output`` bytes
    truncated: bool
    backend: str
    seconds: float


def _real(path: Path | str) -> Path:
    return Path(os.path.realpath(path))


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _validated(spec: Spec) -> tuple[Path, Path, Path, list[Path], list[Path]]:
    """Resolve and check every path. Refuse an allowed path that equals, contains or lies inside a forbidden one."""
    if not spec.argv or not all(isinstance(a, str) and "\x00" not in a for a in spec.argv):
        raise SandboxError("argv must be a non-empty list of strings")
    if not os.path.isabs(spec.argv[0]):
        raise SandboxError("argv[0] must be an absolute path")
    if not (0 < spec.timeout <= 6 * 3600) or not (0 < spec.max_output <= 16 * 1024 * 1024):
        raise SandboxError("timeout or max_output out of range")
    export, out, tmp = (_real(p) for p in (spec.export_dir, spec.out_dir, spec.tmp_dir))
    tool = [_real(p) for p in spec.toolchain_ro]
    forb = [_real(p) for p in spec.forbidden]
    for p in (export, out, tmp, *tool, *forb):
        if not _SAFE_PATH.match(str(p)):
            raise SandboxError("path contains characters the sandbox profile cannot carry")
    for p in (export, out, tmp):
        if not p.is_dir():
            raise SandboxError(f"not a directory: {p.name}")
    for i, a in enumerate((export, out, tmp)):
        for b in (export, out, tmp)[i + 1 :]:
            if _overlaps(a, b):
                raise SandboxError("export, out and tmp must be disjoint")
    for p in (export, out, tmp, *tool):
        if p == Path("/") or any(_overlaps(p, f) for f in forb):
            raise SandboxError("an allowed path overlaps a forbidden one")
    home = _real(Path.home())
    for p in (out, tmp, *tool):  # nothing writable (or toolchain) may be the user's home or a parent of it
        if p == home or p in home.parents:
            raise SandboxError("an allowed path contains the home directory")
    return export, out, tmp, tool, forb


def build_env(spec: Spec, *, home: str, out: str, export: str, tool: Iterable[Path]) -> dict[str, str]:
    """The child environment, from scratch. ``PATH`` is the toolchain ``bin`` dirs plus the system ones."""
    for name, value in spec.env.items():
        if name not in ENV_ALLOWED:
            raise SandboxError(f"environment name not allowed: {name}")
        if not isinstance(value, str) or not _SAFE_VALUE.match(value):
            raise SandboxError(f"environment value not allowed for {name}")
    bins = [str(p / "bin") for p in tool if (p / "bin").is_dir()] + [str(p) for p in tool if p.is_dir()]
    env = {
        "PATH": ":".join([*bins, *_SYSTEM_BIN]),
        "HOME": home,
        "TMPDIR": home,
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "CI": "1",
        "ORCH_LAND_EXPORT": export,
        "ORCH_LAND_OUT": out,
    }
    env.update(spec.env)
    return env


# ----------------------------------------------------------------------------------------------- macOS (Seatbelt)


def _sb(path: Path | str) -> str:
    return f'"{path}"'


def _ancestors(paths: Iterable[Path]) -> list[str]:
    seen: dict[str, None] = {}
    for p in paths:
        for parent in p.parents:
            seen[str(parent)] = None
    return list(seen)


def seatbelt_profile(export: Path, out: Path, tmp: Path, tool: Sequence[Path], forb: Sequence[Path]) -> str:
    """The Seatbelt profile text. Deny by default; the trailing denies win over every allow (last match wins)."""
    read = [export, out, tmp, *tool]
    lines = [
        "(version 1)",
        "(deny default)",
        "(deny network*)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow file-read* " + " ".join(f"(subpath {_sb(p)})" for p in (*read, *map(Path, _MAC_SYSTEM_READ))) + ")",
        # path traversal needs metadata of the ancestors only (existence of the allowed directories themselves)
        '(allow file-read-data (literal "/"))',  # dyld reads the root directory entry itself, nothing below it
        "(allow file-read-metadata " + " ".join(f"(literal {_sb(a)})" for a in ["/", *_ancestors(read)]) + ")",
        "(allow file-write* " + " ".join(f"(subpath {_sb(p)})" for p in (out, tmp)) + ")",
        '(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper"))',
        "(allow file-map-executable "
        + " ".join(f"(subpath {_sb(p)})" for p in (*tool, *map(Path, _MAC_SYSTEM_READ[:5])))
        + ")",
        "(allow process-exec "
        + " ".join(f"(subpath {_sb(p)})" for p in (*tool, export, *map(Path, ("/usr", "/bin", "/sbin"))))
        + ")",
        "(deny process-exec " + " ".join(f"(literal {_sb(p)})" for p in _MAC_DENY_EXEC) + ")",
        "(deny network* (remote unix-socket) (local unix-socket))",
    ]
    if forb:
        lines.append("(deny file* " + " ".join(f"(subpath {_sb(p)})" for p in forb) + ")")
    return "\n".join(lines) + "\n"


def _seatbelt_argv(spec: Spec) -> tuple[list[str], dict[str, str], Path]:
    export, out, tmp, tool, forb = _validated(spec)
    profile = seatbelt_profile(export, out, tmp, tool, forb)
    env = build_env(spec, home=str(tmp), out=str(out), export=str(export), tool=tool)
    return [SANDBOX_EXEC, "-p", profile, *spec.argv], env, export


# ----------------------------------------------------------------------------------------------- Linux (bubblewrap)


def bwrap_argv(bwrap: str, spec: Spec) -> tuple[list[str], Path]:
    """The full ``bwrap`` command line. Inside: export at ``/work`` (ro), out at ``/out``, tmp at ``/tmp`` (=HOME)."""
    export, out, tmp, tool, _forb = _validated(spec)  # forbidden paths are simply never mounted
    env = build_env(spec, home="/tmp", out="/out", export="/work", tool=tool)
    argv = [
        bwrap, "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind-try", "/bin", "/bin", "--ro-bind-try", "/sbin", "/sbin",
        "--ro-bind-try", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
        "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
        "--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
        "--ro-bind-try", "/etc/ssl/certs", "/etc/ssl/certs",
        "--proc", "/proc", "--dev", "/dev",
        "--ro-bind", str(export), "/work", "--bind", str(out), "/out", "--bind", str(tmp), "/tmp",
    ]  # fmt: skip
    for p in tool:
        argv += ["--ro-bind", str(p), str(p)]
    for k, v in env.items():
        argv += ["--setenv", k, v]
    argv += ["--chdir", "/work", "--", *spec.argv]
    return argv, Path("/")


# ----------------------------------------------------------------------------------------------- backend choice


def _bwrap_path() -> str | None:
    for cand in BWRAP_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _true_binary() -> str | None:
    for cand in ("/usr/bin/true", "/bin/true"):
        if os.path.exists(cand):
            return cand
    return None


def _selftest(backend: str) -> bool:
    """Run ``true`` under the backend with a throwaway layout. A backend that cannot start is not available."""
    true = _true_binary()
    if true is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="orch-sbx-") as d:
            root = _real(d)
            for n in ("export", "out", "tmp"):
                (root / n).mkdir()
            spec = Spec(argv=[true], export_dir=root / "export", out_dir=root / "out", tmp_dir=root / "tmp", timeout=20)
            res = _execute(backend, spec)
            return res.returncode == 0 and not res.timed_out
    except (OSError, SandboxError):
        return False


def available_backend() -> str | None:
    """``"sandbox-exec"``, ``"bwrap"`` or ``None``; only a backend whose self-test passes counts."""
    if sys.platform == "darwin":
        order = ["sandbox-exec"] if os.path.exists(SANDBOX_EXEC) else []
    else:
        order = ["bwrap"] if _bwrap_path() else []
    for name in order:
        if _selftest(name):
            return name
    return None


# ----------------------------------------------------------------------------------------------- running


def _killpg(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _spawn_argv(backend: str, spec: Spec) -> tuple[list[str], dict[str, str], Path]:
    if backend == "sandbox-exec":
        return _seatbelt_argv(spec)
    if backend == "bwrap":
        bwrap = _bwrap_path()
        if bwrap is None:
            raise NoSandbox("bwrap not found")
        argv, cwd = bwrap_argv(bwrap, spec)
        return argv, {}, cwd  # bwrap's own env is empty; the child's is set with --setenv
    raise NoSandbox(f"unknown backend {backend}")


def _execute(backend: str, spec: Spec) -> Result:
    argv, env, cwd = _spawn_argv(backend, spec)
    started = time.monotonic()
    deadline = started + spec.timeout
    proc = subprocess.Popen(  # noqa: S603 - fixed sandbox argv, no shell
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=str(cwd),
        close_fds=True, start_new_session=True,
    )  # fmt: skip
    pgid = proc.pid
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    chunks: list[bytes] = []
    kept = 0
    truncated = False
    timed_out = False

    def pull() -> bool:
        """Read one chunk; False at EOF. Bytes past the cap are dropped (the pipe keeps draining)."""
        nonlocal kept, truncated
        data = os.read(fd, 65536)
        if not data:
            return False
        room = spec.max_output - kept
        if room > 0:
            chunks.append(data[:room])
            kept += min(len(data), room)
        if len(data) > room:
            truncated = True
        return True

    try:
        open_ = True
        while open_:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            if fdwait.readable(fd, min(left, 0.2)):
                open_ = pull()
            elif proc.poll() is not None:
                break
        _killpg(pgid)  # on timeout, and on exit: nothing the check started survives the result
        proc.wait()
        # the group is dead, so the pipe reaches EOF; drain what is already there
        end = time.monotonic() + 2.0
        while open_ and time.monotonic() < end and fdwait.readable(fd, 0.2):
            open_ = pull()
    finally:
        _killpg(pgid)
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return Result(
        returncode=proc.returncode, timed_out=timed_out, output=b"".join(chunks), truncated=truncated,
        backend=backend, seconds=time.monotonic() - started,
    )  # fmt: skip


def run_check(spec: Spec, *, backend: str | None = None) -> Result:
    """Run one check in the OS sandbox, or raise :class:`NoSandbox`. Never runs without one.

    ``backend`` is for tests (to force one); it must still pass the self-test, so a forced backend cannot bypass it."""
    chosen = available_backend() if backend is None else (backend if _selftest(backend) else None)
    if chosen is None:
        raise NoSandbox("no usable OS sandbox (sandbox-exec on macOS, bwrap on Linux)")
    return _execute(chosen, spec)
