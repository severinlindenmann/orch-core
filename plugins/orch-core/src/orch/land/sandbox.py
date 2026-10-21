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
  Network: the private network namespace has only its own loopback and no route, so sockets can be created but reach
  only the check itself (host loopback services and the host's abstract unix sockets live in the host namespace).

No usable backend means :class:`NoSandbox` (``land.no_sandbox``): a check never runs unsandboxed. A backend counts as
usable only if a trivial command really runs under it (user namespaces can be switched off on a host). Executables are
found at fixed absolute paths, never through ``PATH``.

The child gets: an environment built from scratch (an allowlist, see :data:`ENV_ALLOWED`), no fds above 2, stdin from
``/dev/null``, its own session and process group. The whole group is killed at the timeout and again when the main
process exits, before the result is returned. Output is merged, capped at ``max_output`` bytes, and is data (the caller
redacts it before it reaches a log).

Known limits. macOS: Seatbelt cannot stop a same-UID process from reading other processes' arguments and environment
(``sysctl KERN_PROCARGS2``; probed 21 Oct: allowed even under ``(deny sysctl*)``). Acceptable ONLY because checks have
no network: check output stays host-local and redacted and is never shown to other members. It becomes a BLOCKER for
open question N4 (network during checks) until #287 (separate user). The process calling :func:`run_check` must hold no
secret in its argv or environment. ``process-info*`` of other processes and ``/bin/launchctl`` are denied.
macOS has no pid namespace: a descendant that calls ``setsid`` leaves the process group and can outlive the check,
still inside the sandbox. Callers therefore use a fresh, never-reused out/tmp per run, read results only through
:func:`read_out_file`, and remove the trees fd-based. Follow-up: kill sandboxed same-UID processes that can still write
this run's tmp (``sandbox_check``). The Linux backend has none of this.
Residual (same UID): a path validated here can be retargeted through a symlink the same user controls between
:func:`_validated` and launch; closing it needs descriptor-pinned paths or a separate user (#287).
"""

from __future__ import annotations

import contextlib
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
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
    "/usr", "/bin", "/sbin", "/System/Library", "/System/Cryptexes", "/Library/Apple", "/Library/Preferences/Logging",
    "/private/var/select", "/private/var/db/timezone", "/private/var/db/dyld",
    "/private/etc/ssl", "/private/etc/localtime",
    "/private/etc/passwd", "/private/etc/group", "/private/etc/resolv.conf", "/private/etc/hosts",
    "/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/dtracehelper",
)  # fmt: skip
_MAC_DENY_EXEC = (
    "/usr/bin/security",
    "/usr/bin/ssh-add",
    "/usr/bin/ssh-agent",
    "/bin/launchctl",
)  # defence in depth; mach is denied too


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
    tampered: bool = False  # out/tmp held a hard-linked or special file afterwards: the check failed, read nothing

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.tampered


def _canon_existing(path: str) -> str:
    """The kernel's own spelling of an existing path: on macOS ``F_GETPATH`` (on-disk case, no /System/Volumes/Data
    firmlink prefix); elsewhere ``realpath``. Seatbelt matches ``subpath`` against that spelling."""
    real = os.path.realpath(path)
    if sys.platform != "darwin":
        return real
    import fcntl

    fd = os.open(real, os.O_RDONLY | getattr(os, "O_SYMLINK", 0) | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        got = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
    finally:
        os.close(fd)
    return os.fsdecode(got.split(b"\0", 1)[0])


def _real(path: Path | str) -> Path:
    """Canonical path. Components are resolved one by one (symlinks before ``..``). A component that exists but cannot
    be resolved (a loop, a dangling link in the middle or at the end) is refused: fail closed. Only a genuinely missing
    tail (``lstat`` says ENOENT on a non-link) is appended to the canonical spelling of what exists."""
    raw = os.fspath(path)
    cur = "/"
    parts = [x for x in raw.split("/") if x]
    if not raw.startswith("/"):
        cur = os.getcwd()
    for i, comp in enumerate(parts):
        if comp == ".":
            continue
        if comp == "..":
            cur = os.path.dirname(cur)
            continue
        nxt = os.path.join(cur, comp)
        try:
            st = os.lstat(nxt)
        except FileNotFoundError:
            tail = [c for c in parts[i:] if c not in (".",)]
            if ".." in tail:
                raise SandboxError("a path has '..' after a missing component") from None
            return Path(_canon_existing(cur), *tail)
        except OSError as e:
            raise SandboxError("a path component cannot be examined") from e
        if stat.S_ISLNK(st.st_mode):
            try:
                nxt = os.path.realpath(nxt, strict=True)
            except OSError as e:
                raise SandboxError("a path has a loop or dangling symlink") from e
        cur = nxt
    return Path(_canon_existing(cur))


def _fold(p: Path) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFD", x).casefold() for x in p.parts)


def _overlaps(a: Path, b: Path) -> bool:
    """Equal or one inside the other, comparing case-insensitively and NFD-normalised (APFS/HFS+ do)."""
    fa, fb = _fold(a), _fold(b)
    n = min(len(fa), len(fb))
    return fa[:n] == fb[:n]


def _account_home() -> Path:
    """The account's home directory from the user database, whatever ``$HOME`` says."""
    return _real(pwd.getpwuid(os.getuid()).pw_dir)


_ACCOUNT_SECRETS = (".ssh", ".gnupg", ".aws", ".config", ".netrc", ".kube", ".docker", "Library/Keychains")
# what the backends mount from the host besides the caller's paths (a forbidden path inside one would be exposed)
_SYSTEM_ROOTS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/System/Library", "/System/Cryptexes", "/Library/Apple", "/etc",
    "/private/etc", "/private/var/select", "/private/var/db", "/dev", "/proc",
)  # fmt: skip


def _secret_paths() -> list[Path]:
    """Paths that are always forbidden: the account's credential directories (and ``$HOME``'s, if it differs)."""
    homes = {_account_home(), _real(Path.home())}
    out: dict[Path, None] = {}
    for h in homes:
        for n in _ACCOUNT_SECRETS:
            out[h / n] = None  # the literal spelling (what the profile sees if it is a symlink)
            try:
                out[_real(h / n)] = None  # and where it really points (~/.ssh -> ~/dotfiles/ssh)
            except SandboxError:
                pass
    return list(out)


def _private_dir(raw: Path | str, what: str) -> Path:
    """A writable directory must be one the caller made: not a symlink, ours, not writable by group or others."""
    try:
        st = os.lstat(raw)
    except OSError as e:
        raise SandboxError(f"{what} is not a directory") from e
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SandboxError(f"{what} must be a real directory, not a symlink")
    if st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise SandboxError(f"{what} must be owned by this user and not writable by group or others")
    return _real(raw)


_SCAN_LIMIT = 500_000


def _scan_aliases(root: Path, what: str) -> None:
    """Refuse a tree holding a socket, device, FIFO or a file with more than one link. A hard link would let one inode
    be reached under two names (a secret planted into out/tmp, or an export file rewritten through out/tmp), and a
    pathname socket stays connectable inside a network namespace."""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in [*dirnames, *filenames]:
            seen += 1
            if seen > _SCAN_LIMIT:
                raise SandboxError(f"{what} holds too many entries")
            st = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISLNK(st.st_mode) or stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise SandboxError(f"{what} holds a special file (socket, device or fifo)")
            if st.st_nlink > 1:
                raise SandboxError(f"{what} holds a hard-linked file")


def _validated(spec: Spec) -> tuple[Path, Path, Path, list[Path], list[Path]]:
    """Resolve and check every path. Refuse an allowed path that equals, contains or lies inside a forbidden one."""
    if not spec.argv or not all(isinstance(a, str) and "\x00" not in a for a in spec.argv):
        raise SandboxError("argv must be a non-empty list of strings")
    if not os.path.isabs(spec.argv[0]):
        raise SandboxError("argv[0] must be an absolute path")
    if not (0 < spec.timeout <= 6 * 3600) or not (0 < spec.max_output <= 16 * 1024 * 1024):
        raise SandboxError("timeout or max_output out of range")
    export = _real(spec.export_dir)
    out = _private_dir(spec.out_dir, "out_dir")
    tmp = _private_dir(spec.tmp_dir, "tmp_dir")
    tool = [_real(p) for p in spec.toolchain_ro]
    forb = [*(_real(p) for p in spec.forbidden), *_secret_paths()]
    for p in (export, out, tmp, *tool, *forb):
        if not _SAFE_PATH.fullmatch(str(p)):
            raise SandboxError("path contains characters the sandbox profile cannot carry")
    if not export.is_dir():
        raise SandboxError("export_dir is not a directory")
    for i, a in enumerate((export, out, tmp)):
        for b in (export, out, tmp)[i + 1 :]:
            if _overlaps(a, b):
                raise SandboxError("export, out and tmp must be disjoint")
    for p in (export, out, tmp, *tool):
        if p == Path("/") or any(_overlaps(p, f) for f in forb):
            raise SandboxError("an allowed path overlaps a forbidden one")
    for f in forb:  # a forbidden path the backend mounts anyway (a key under /usr) would be exposed
        if any(f == r or r in f.parents for r in map(Path, _SYSTEM_ROOTS)):
            raise SandboxError("a forbidden path lies inside a system location the sandbox exposes")
    home = _account_home()
    for p in (out, tmp, *tool):  # nothing writable (or toolchain) may be the user's home or a parent of it
        if p == home or p in home.parents:
            raise SandboxError("an allowed path contains the home directory")
    for p, what in ((export, "export_dir"), (out, "out_dir"), (tmp, "tmp_dir")):
        _scan_aliases(p, what)
    return export, out, tmp, tool, forb


def _tampered(*roots: Path) -> bool:
    """True if any file under the roots is not a plain single-link regular file (or a dir/symlink)."""
    try:
        for root in roots:
            _scan_aliases(root, "tree")
    except (SandboxError, OSError):
        return True
    return False


def read_out_file(out_dir: Path | str, rel: str, *, limit: int = 1024 * 1024) -> bytes:
    """Read one file the check left in ``out_dir``, for the host. The check controls this tree, so: every component is
    opened with ``O_NOFOLLOW`` relative to its parent fd, the final one must be a regular file with a single link, and
    at most ``limit`` bytes are read. Anything else raises :class:`SandboxError`."""
    parts = [x for x in rel.split("/") if x]
    if not parts or any(x in (".", "..") for x in parts) or "\x00" in rel:
        raise SandboxError("bad output path")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(out_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in parts[:-1]:
            nxt = os.open(name, flags | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        try:
            leaf = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=fd)  # NONBLOCK: a fifo must not hang the host
        except OSError as e:
            raise SandboxError("output file cannot be opened (missing or a symlink)") from e
        try:
            st = os.fstat(leaf)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
                raise SandboxError("output file is not a plain single-link regular file")
            return os.read(leaf, limit) if limit else b""
        finally:
            os.close(leaf)
    except OSError as e:
        raise SandboxError("output path cannot be opened") from e
    finally:
        os.close(fd)


def build_env(spec: Spec, *, home: str, out: str, export: str, tool: Iterable[Path]) -> dict[str, str]:
    """The child environment, from scratch. ``PATH`` is the toolchain ``bin`` dirs plus the system ones."""
    for name, value in spec.env.items():
        if name not in ENV_ALLOWED:
            raise SandboxError(f"environment name not allowed: {name}")
        if not isinstance(value, str) or not _SAFE_VALUE.fullmatch(value):
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
        "(deny process-info*)",  # no proc_pidpath / ps of other processes
        "(allow process-info* (target self))",
        "(deny process-exec " + " ".join(f"(literal {_sb(p)})" for p in _MAC_DENY_EXEC) + ")",
        "(deny network* (remote unix-socket) (local unix-socket))",
        # No hard links at all: a link into out/tmp to a file the check may only read (export, toolchain) would let it
        # write through the link into the other inode. Explicit, after the allows (last match wins).
        "(deny file-link)",
        "(deny file-write* " + " ".join(f"(subpath {_sb(p)})" for p in (export, *tool)) + ")",
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


def _trusted_exe(path: str) -> bool:
    """A backend binary must be root-owned with no group/other write bit, and so must every directory above it: a
    user-writable ``bwrap`` (Homebrew) could be swapped for a wrapper that confines nothing."""
    try:
        real = Path(os.path.realpath(path))
        if not real.is_file() or not os.access(real, os.X_OK):
            return False
        for p in (real, *real.parents):
            st = os.stat(p)
            if st.st_uid != 0 or st.st_mode & 0o022:
                return False
    except OSError:
        return False
    return True


def _bwrap_path() -> str | None:
    for cand in BWRAP_CANDIDATES:
        if os.path.isfile(cand) and _trusted_exe(cand):
            return cand
    return None


def _true_binary() -> str | None:
    for cand in ("/usr/bin/true", "/bin/true"):
        if os.path.exists(cand):
            return cand
    return None


def _selftest(backend: str) -> bool:
    """The backend must start AND confine: ``true`` has to succeed, and ``cat`` of a canary file that lies outside every
    allowed path has to fail without printing it. A wrapper that merely exits 0 is not a sandbox."""
    true, cat = _true_binary(), next((c for c in ("/bin/cat", "/usr/bin/cat") if os.path.exists(c)), None)
    if true is None or cat is None:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="orch-sbx-") as d:
            root = _real(d)
            for n in ("export", "out", "tmp", "outside"):
                (root / n).mkdir(mode=0o700)
            (root / "outside" / "canary").write_text("orch-sandbox-canary")
            base = {"export_dir": root / "export", "out_dir": root / "out", "tmp_dir": root / "tmp", "timeout": 20}
            ok = _execute(backend, Spec(argv=[true], **base))
            bad = _execute(backend, Spec(argv=[cat, str(root / "outside" / "canary")], **base))
            return (
                ok.returncode == 0 and not ok.timed_out
                and bad.returncode != 0 and b"orch-sandbox-canary" not in bad.output
            )  # fmt: skip
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


def _exited(pid: int) -> bool:
    """True once the child has exited, WITHOUT reaping it (the zombie keeps its pgid reserved)."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _killpg(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _spawn_argv(backend: str, spec: Spec) -> tuple[list[str], dict[str, str], Path]:
    if backend == "sandbox-exec":
        if not _trusted_exe(SANDBOX_EXEC):
            raise NoSandbox("sandbox-exec is missing or not root-owned")
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
            if open_ and _exited(proc.pid):
                break
        _killpg(pgid)  # on timeout, and on exit: nothing the check started survives the result
        proc.wait()
        # the group is dead, so the pipe reaches EOF; drain what is already there
        end = time.monotonic() + 2.0
        while open_ and time.monotonic() < end and fdwait.readable(fd, 0.2):
            open_ = pull()
    finally:
        if proc.returncode is None:  # once reaped the pgid may be reused by an unrelated group
            _killpg(pgid)
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return Result(
        returncode=proc.returncode, timed_out=timed_out, output=b"".join(chunks), truncated=truncated,
        backend=backend, seconds=time.monotonic() - started,
        tampered=_tampered(_real(spec.out_dir), _real(spec.tmp_dir)),
    )  # fmt: skip


def run_check(spec: Spec, *, backend: str | None = None) -> Result:
    """Run one check in the OS sandbox, or raise :class:`NoSandbox`. Never runs without one.

    ``backend`` is for tests (to force one); it must still pass the self-test, so a forced backend cannot bypass it."""
    chosen = available_backend() if backend is None else (backend if _selftest(backend) else None)
    if chosen is None:
        raise NoSandbox("no usable OS sandbox (sandbox-exec on macOS, bwrap on Linux)")
    return _execute(chosen, spec)
