"""The AI Factory runner inside the dashboard server (docs/factory.md, phase 4): a background round every few seconds
that calls orch.core.factory_runner.tick. It does nothing unless the factory is on, tmux exists, and the human started
an epic from the dashboard.

Its sessions run on a tmux server of their own, on a socket inside the guarded permits folder of the orch config dir
(`tmux -S <config>/permits/tmux/<random>/factory`, folder 0700), not on the Terminals' named socket. A same-user process is not
isolated from that socket by the operating system; the guard only makes the obvious routes to it fail."""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from pathlib import Path

from orch.core import factory_runner
from orch.dashboard import launch
from orch.dashboard.views import HUMAN
from orch.errors import OrchError, UsageError

log = logging.getLogger("orch.factory")
ROUND_SECONDS = 15
_NO_SERVER = ("no server running", "No such file or directory", "no sessions")


def _folder_name(permits: Path) -> str:
    """The random name of the folder that holds the socket, kept only in a permits file (created once, exclusively)."""
    import re
    import secrets
    f = permits / "tmux.name"
    for _ in range(2):
        try:
            name = f.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[0-9a-f]{16}", name):
                return name
        except OSError:
            pass
        try:
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as h:
            h.write(secrets.token_hex(8))
    raise UsageError("the factory's tmux folder name cannot be read")


def socket_path() -> Path:
    """The factory's tmux socket: in a random-named folder (0700) of permits/tmux, so a listing of the folder above does
    not show where it is. Every folder on the way must be ours alone."""
    permits = launch.config_dir() / "permits"
    permits.mkdir(mode=0o700, parents=True, exist_ok=True)
    d = permits / "tmux" / _folder_name(permits)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    for x in (d.parent, d):
        st = x.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            os.chmod(x, 0o700)
    return d / "factory"


def available() -> bool:
    return factory_runner.resolve_bin("tmux") is not None


def _tmux(args: list[str], timeout: float = 10) -> subprocess.CompletedProcess:
    """One tmux command on the factory socket, by the resolved program, with the fixed environment. Tests replace it."""
    tmux = factory_runner.resolve_bin("tmux")
    if tmux is None:
        raise UsageError("tmux was not found at a trusted path")
    env = {"PATH": factory_runner.child_path(tmux), "LC_ALL": "C"}
    return subprocess.run([tmux, "-S", str(socket_path()), *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env)


class TmuxLauncher:
    def alive(self) -> set[str] | None:
        try:
            r = _tmux(["list-sessions", "-F", "#{session_name}"])
        except (OSError, subprocess.TimeoutExpired, UsageError):
            return None
        if r.returncode == 0:
            return set(r.stdout.split())
        # only "there is no server" is an answer; any other failure leaves what we knew as it was
        return set() if any(m in r.stderr for m in _NO_SERVER) else None

    def start(self, name: str, cwd: str, argv: list[str]) -> int:
        # argv already starts with `env -i ...`: the session's shell command holds nothing of the server's environment
        r = _tmux(["new-session", "-d", "-s", name, "-c", launch.tmux_arg(cwd), "-x", "160", "-y", "45",
                   launch.tmux_arg(shlex.join(argv))])
        if r.returncode != 0:
            raise UsageError(f"tmux could not start a session named {name}")
        r = _tmux(["display-message", "-p", "-t", f"={name}:", "#{pane_pid}"])
        return int(r.stdout.strip())  # ValueError (no pid) ends the session in the caller: nothing runs unbound

    def stop(self, name: str) -> None:
        if _tmux(["kill-session", "-t", f"={name}"]).returncode != 0:
            raise UsageError(f"{name} could not be ended (has it ended already?)")


def run_once(ws, launcher=None) -> list[str]:
    from orch.core import factory_sessions, permits
    if not available() or not (permits.enabled(ws) or factory_sessions.bindings(ws)):
        return []
    return factory_runner.tick(ws, HUMAN, launcher or TmuxLauncher(), settings=launch.load_settings())


def startup(ws, launcher=None) -> list[str]:
    """Dashboard start: a binding whose session is not running ends now."""
    from orch.core import factory_sessions
    if not factory_sessions.bindings(ws) or not available():
        return []
    return factory_runner.sweep(ws, HUMAN, launcher or TmuxLauncher())


def shutdown(ws, launcher=None) -> list[str]:
    """Dashboard stop: every factory session stops and every binding ends (a child may start again next time)."""
    from orch.core import factory_sessions
    if not factory_sessions.bindings(ws):
        return []
    return factory_runner.sweep(ws, HUMAN, launcher or TmuxLauncher(), stop_all=True)


async def loop(ws, seconds: float = ROUND_SECONDS) -> None:
    try:
        for line in await asyncio.to_thread(startup, ws):
            log.info("factory runner: %s", line)
    except Exception:
        log.exception("factory runner startup failed")
    try:
        while True:
            await asyncio.sleep(seconds)
            try:
                for line in await asyncio.to_thread(run_once, ws):
                    log.info("factory runner: %s", line)
            except (OrchError, OSError) as e:  # e.g. refused under an agent harness: say so, keep serving
                log.warning("factory runner: %s", e)
            except Exception:  # a round must never take the dashboard down
                log.exception("factory runner round failed")
    finally:
        try:
            for line in await asyncio.shield(asyncio.to_thread(shutdown, ws)):
                log.info("factory runner: %s", line)
        except BaseException:  # never block the dashboard's own shutdown
            log.exception("factory runner shutdown failed")
