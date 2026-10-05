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
from orch.dashboard.reach import LOCAL_HUMAN
from orch.errors import OrchError, UsageError

log = logging.getLogger("orch.factory")
ROUND_SECONDS = 15
TYPE_POLLS, TYPE_POLL_SECONDS = 5, 0.2  # after typing a nudge, read the pane this often before giving up
_sleep = __import__("time").sleep  # tests stand in for it
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


def _tmux(args: list[str], timeout: float = 10, socket: Path | None = None) -> subprocess.CompletedProcess:
    """One tmux command on the factory socket (or `socket`, another one in the same private folder, such as the
    schedules'), by the resolved program, with the fixed environment. Tests replace it."""
    tmux = factory_runner.resolve_bin("tmux")
    if tmux is None:
        raise UsageError("tmux was not found at a trusted path")
    env = {"PATH": factory_runner.child_path(tmux), "LC_ALL": "C"}
    return subprocess.run([tmux, "-S", str(socket or socket_path()), *args], capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL, env=env)


class TmuxLauncher:
    socket: Path | None = None  # None: the factory's own socket

    def _tmux(self, args: list[str]) -> subprocess.CompletedProcess:
        return _tmux(args, socket=self.socket) if self.socket is not None else _tmux(args)

    def alive(self) -> set[str] | None:
        """Sessions whose pane still runs. Panes stay after their process exits (remain-on-exit, set at start), so
        the runner can read how a session ended (reap) before it ends the session."""
        try:
            r = self._tmux(["list-panes", "-a", "-F", "#{session_name} #{pane_dead}"])
        except (OSError, subprocess.TimeoutExpired, UsageError):
            return None
        if r.returncode == 0:
            return {ln.rsplit(" ", 1)[0] for ln in r.stdout.splitlines() if ln.endswith(" 0")}
        # only "there is no server" is an answer; any other failure leaves what we knew as it was
        return set() if any(m in r.stderr for m in _NO_SERVER) else None

    def reap(self, name: str) -> tuple[str, str] | None:
        """(exit status, the pane's text) of a session whose process ended, and the session is ended; None when it is
        not there or still runs (then nothing is ended)."""
        try:
            r = self._tmux(["display-message", "-p", "-t", f"={name}:", "#{pane_dead} #{pane_dead_status}"])
            dead, _, status = r.stdout.strip().partition(" ")
            if r.returncode != 0 or dead != "1":
                return None
            text = self._tmux(["capture-pane", "-p", "-J", "-t", f"={name}:", "-S", "-200"]).stdout
            self._tmux(["kill-session", "-t", f"={name}"])
        except (OSError, subprocess.TimeoutExpired, UsageError):
            return None
        return status, "\n".join(ln for ln in text.splitlines() if not ln.startswith("Pane is dead"))

    def capture(self, name: str) -> str | None:
        """The visible text of the session's pane (plain, no escapes), or None."""
        try:
            r = self._tmux(["capture-pane", "-p", "-t", f"={name}:"])
        except (OSError, subprocess.TimeoutExpired, UsageError):
            return None
        return r.stdout if r.returncode == 0 else None

    def type(self, name: str, text: str) -> bool:
        """Type one of the runner's built-in nudges into the pane: only onto an empty input line (read first), then
        read the pane again a few times over about a second (tmux redraws asynchronously) and press Enter only when
        the text sits on the input line itself and nothing Enter would answer instead is on screen (typed_ok);
        otherwise clear the input line (C-u) and press nothing more. True when Enter was pressed. Nothing else is
        ever typed."""
        if text not in factory_runner.NUDGES.values():
            raise UsageError("the runner types only its built-in nudges")
        if factory_runner.input_line(self.capture(name)) != "":
            return False
        if self._tmux(["send-keys", "-t", f"={name}:", "-l", "--", text]).returncode != 0:
            raise UsageError(f"could not type into {name}")
        for _ in range(TYPE_POLLS):
            _sleep(TYPE_POLL_SECONDS)
            if factory_runner.typed_ok(self.capture(name), text):
                return self._tmux(["send-keys", "-t", f"={name}:", "Enter"]).returncode == 0
        self._tmux(["send-keys", "-t", f"={name}:", "C-u"])
        return False

    def start(self, name: str, cwd: str, argv: list[str]) -> int:
        # argv already starts with `env -i ...`: the session's shell command holds nothing of the server's environment.
        # remain-on-exit is set (server-wide, in the same tmux call, before the session exists) so a session that ends
        # right away leaves its last screen and exit status for reap().
        r = self._tmux(["start-server", ";", "set-option", "-g", "-w", "remain-on-exit", "on", ";",
                        "new-session", "-d", "-s", name, "-c", launch.tmux_arg(cwd), "-x", "160", "-y", "45",
                        launch.tmux_arg(shlex.join(argv))])
        if r.returncode != 0:
            raise UsageError(f"tmux could not start a session named {name}")
        r = self._tmux(["display-message", "-p", "-t", f"={name}:", "#{pane_pid}"])
        return int(r.stdout.strip())  # ValueError (no pid) ends the session in the caller: nothing runs unbound

    def stop(self, name: str) -> None:
        if self._tmux(["kill-session", "-t", f"={name}"]).returncode != 0:
            raise UsageError(f"{name} could not be ended (has it ended already?)")


def run_once(ws, launcher=None) -> list[str]:
    from orch.core import factory_sessions, permits
    if not available() or not (permits.enabled(ws) or factory_sessions.bindings(ws)):
        return []
    return factory_runner.tick(ws, LOCAL_HUMAN, launcher or TmuxLauncher(), settings=launch.load_settings())


def release_once(ws, run=None) -> list[str]:
    """One release round (phase 6, orch.core.factory_release): Ready Dark epics whose charter signs a release go
    through the human's recipe. Needs no tmux; nothing unless the factory is on."""
    from orch.core import factory_release, permits
    if not permits.enabled(ws):
        return []
    return factory_release.tick(ws, LOCAL_HUMAN, run)


async def _release_loop(ws, seconds: float) -> None:
    """Releases in a round of their own: a stage's command may run for minutes, and the session round (pauses,
    stops, launches) must not wait for it."""
    while True:
        await asyncio.sleep(seconds)
        try:
            for line in await asyncio.to_thread(release_once, ws):
                log.info("factory release: %s", line)
        except (OrchError, OSError) as e:
            log.warning("factory release: %s", e)
        except Exception:  # a round must never take the dashboard down
            log.exception("factory release round failed")


def startup(ws, launcher=None) -> list[str]:
    """Dashboard start: a binding whose session is not running ends now."""
    from orch.core import factory_sessions
    if not factory_sessions.bindings(ws) or not available():
        return []
    return factory_runner.sweep(ws, LOCAL_HUMAN, launcher or TmuxLauncher())


def shutdown(ws, launcher=None) -> list[str]:
    """Dashboard stop: every factory session stops and every binding ends (a child may start again next time)."""
    from orch.core import factory_sessions
    if not factory_sessions.bindings(ws):
        return []
    return factory_runner.sweep(ws, LOCAL_HUMAN, launcher or TmuxLauncher(), stop_all=True)


async def loop(ws, seconds: float = ROUND_SECONDS) -> None:
    from orch.core import factory_release
    factory_release._STOPPING.clear()  # a dashboard started again in this process releases again
    releases = asyncio.create_task(_release_loop(ws, seconds))
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
        # a running release command's process group gets SIGTERM, then SIGKILL after a short grace (its outcome is
        # recorded as failed by its own thread), instead of holding the shutdown for its timeout
        try:
            await asyncio.shield(asyncio.to_thread(factory_release.terminate_all))
        except BaseException:
            log.exception("factory release shutdown failed")
        releases.cancel()
        try:
            for line in await asyncio.shield(asyncio.to_thread(shutdown, ws)):
                log.info("factory runner: %s", line)
        except BaseException:  # never block the dashboard's own shutdown
            log.exception("factory runner shutdown failed")
