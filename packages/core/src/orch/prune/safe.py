"""The outside world of prune, made safe: programs by absolute path, a scrubbed environment, a neutral working
directory, answers from the person's own terminal, "is this an agent?", and the real macOS keychain seam."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orch import childenv

__all__ = [
    "RUNNER_ENV",
    "agent_reason",
    "resolve_bin",
    "safe_run",
    "scrubbed_env",
    "tty_ask",
    "tty_available",
    "real_keychain",
]

#: set by the runner for an agent's process (and the grant): any of them means "an agent is asking"
RUNNER_ENV = ("ORCH_GRANT", "ORCH_SESSION", "ORCH_TICKET", "ORCH_WORKDIR", "ORCH_HARNESS")
_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin")
_DROP_PREFIXES = ("GIT_", "PYTHON", "LD_", "DYLD_")
_GIT_SAFE = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never")


def agent_reason(env: Mapping[str, str], euid: int | None = None) -> str | None:
    """Why this process is an agent's (so prune refuses), or ``None``."""
    for k in RUNNER_ENV:
        if env.get(k):
            return f"{k} is set"
    try:
        import pwd

        name = pwd.getpwuid(os.geteuid() if euid is None else euid).pw_name
    except (ImportError, KeyError):
        return None
    from orch.setup.osusers import AGENT_PREFIX, SLOT_PREFIX

    if name.startswith((AGENT_PREFIX, SLOT_PREFIX)):
        return f"this process runs as the agent account {name}"
    return None


def scrubbed_env(env: Mapping[str, str], path: str) -> dict[str, str]:
    out = {k: v for k, v in childenv.sanitize(env).items() if not k.startswith(_DROP_PREFIXES)}
    out["PATH"] = path
    return out


def _under(p: Path, roots: Sequence[Path]) -> bool:
    return any(p == r or r in p.parents for r in roots)


def safe_path(env: Mapping[str, str], deny: Sequence[Path]) -> str:
    """``PATH`` reduced to absolute entries that lie under no workspace or state root."""
    keep = []
    for d in (env.get("PATH") or "").split(os.pathsep):
        if d and os.path.isabs(d) and not _under(Path(os.path.abspath(d)), deny):
            keep.append(d)
    return os.pathsep.join(keep)


def resolve_bin(name: str, env: Mapping[str, str], deny: Sequence[Path]) -> str | None:
    """The absolute path of program ``name``: the fixed system folders first, then absolute ``PATH`` entries that are
    not under any root prune may touch (a workspace's ``bin/`` never runs)."""
    if os.path.isabs(name):
        return name if not _under(Path(name), deny) else None
    dirs = [*_SYSTEM_DIRS, *safe_path(env, deny).split(os.pathsep)]
    for d in dirs:
        cand = os.path.join(d, name)
        if d and os.path.isfile(cand) and os.access(cand, os.X_OK) and not _under(Path(cand), deny):
            return cand
    return None


def safe_run(
    argv: Sequence[str],
    env: Mapping[str, str],
    deny: Sequence[Path],
    *,
    extra_env: Mapping[str, str] | None = None,
    timeout: float = 60.0,
) -> tuple[int, str]:
    """Run a program with an absolute path, ``cwd`` = the home folder, a scrubbed environment (no ``GIT_*``,
    ``PYTHON*``, ``LD_*``, ``DYLD_*``) and, for git, no fsmonitor, no hooks and no network protocols."""
    exe = resolve_bin(argv[0], env, deny)
    if exe is None:
        return 127, f"{argv[0]}: not found (only absolute, non-workspace places are searched)"
    args = [exe, *argv[1:]]
    if os.path.basename(exe) == "git":
        args = [exe, *_GIT_SAFE, *argv[1:]]
    home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
    child = scrubbed_env({**env, **(extra_env or {})}, safe_path(env, deny))
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False, cwd=home, env=child, errors="replace"
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ------------------------------------------------------------------------------------------------------- terminal


def tty_available() -> bool:
    """Whether a controlling terminal can be opened: the person's own, never stdin (a pipe, an agent's pty-less run)."""
    if os.name == "nt":
        return sys.stdin is not None and sys.stdin.isatty()
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return False
    os.close(fd)
    return True


def tty_ask(prompt: str) -> str:
    """Write ``prompt`` to and read the answer from ``/dev/tty``; ``""`` when there is no terminal."""
    if os.name == "nt":
        try:
            return input(prompt)
        except EOFError:
            return ""
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return ""
    try:
        os.write(fd, prompt.encode("utf-8", "replace"))
        line = b""
        while not line.endswith(b"\n"):
            chunk = os.read(fd, 1)
            if not chunk:
                break
            line += chunk
        return line.decode("utf-8", "replace").strip()
    except OSError:
        return ""
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------------- keychain


class _Handle:
    """The orch device-key items of one keychain file: listing and deletion are both scoped to the service
    ``io.orch.device-key`` by :class:`orch.custody.keychain.KeychainStore` itself."""

    def __init__(self, store: Any) -> None:
        self._s = store

    def accounts(self) -> list[str]:
        return list(self._s.accounts(""))

    def delete(self, account: str) -> None:
        self._s.delete(account)

    def close(self) -> None:
        self._s.close()


def real_keychain(path: str | None) -> _Handle | None:
    """The keychain file at ``path`` (darwin only); raises when it cannot be opened (locked, denied)."""
    if sys.platform != "darwin":
        return None
    from orch.custody.keychain import KeychainStore

    return _Handle(KeychainStore(path))


def runner_run(env: Mapping[str, str], deny: Sequence[Path]) -> Any:
    """A ``subprocess.run`` look-alike for the install runners (launchd, systemd): same safety as :func:`safe_run`."""

    def run(argv: Sequence[str], **kw: Any) -> subprocess.CompletedProcess[Any]:
        exe = resolve_bin(argv[0], env, deny)
        if exe is None:
            raise FileNotFoundError(argv[0])
        home = env.get("HOME") or env.get("USERPROFILE") or os.path.expanduser("~")
        kw.pop("env", None)
        kw.pop("cwd", None)
        return subprocess.run([exe, *argv[1:]], cwd=home, env=scrubbed_env(env, safe_path(env, deny)), **kw)

    return run
