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


_SOCKET: dict = {}  # orch config dir -> the socket path found there (its folder name read and made once)


def socket_path() -> Path:
    """The factory's tmux socket (_socket_path), found once per orch config dir; every call still checks that its two
    folders are ours alone (two stats), so a screen capture per tile does not re-read the name file and re-make the
    folders."""
    key = str(launch.config_dir())
    p = _SOCKET.get(key)
    if p is None or not p.parent.is_dir():
        p = _SOCKET[key] = _socket_path()
        return p
    for x in (p.parent.parent, p.parent):
        st = x.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            os.chmod(x, 0o700)
    return p


def _socket_path() -> Path:
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


# -- the factory's sessions on the Terminals page ----------------------------------------------------------------------
HUMAN_QUIET = 60  # seconds after the human's last key from the browser in which the runner types no nudge
HUMAN_KEYS: dict[str, float] = {}  # session name -> time.monotonic() of the human's last key from the browser


def note_human_keys(name: str) -> None:
    """The human typed into factory session `name` from the browser (the Terminals page): no nudge for a while."""
    import time
    HUMAN_KEYS[name] = time.monotonic()


def human_typed(name: str) -> bool:
    import time
    at = HUMAN_KEYS.get(name)
    return at is not None and time.monotonic() - at < HUMAN_QUIET


def watched(ws) -> list[dict]:
    """The factory sessions of this workspace the Terminals page shows: from the runner's own bindings of this
    workspace only (never a listing of the tmux server), each name checked by the Terminals' name rule:
    [{name, epic, child, planner}], oldest first."""
    from orch.core import factory_sessions
    from orch.dashboard import terminals
    return [{"name": b["name"], "epic": str(b["epic"]).upper(), "child": b["child"],
             "planner": factory_sessions.is_planner(b)}
            for b in factory_sessions.bindings(ws)
            if isinstance(b.get("name"), str) and terminals.NAME.fullmatch(b["name"])]


def find_watched(ws, name: str) -> dict | None:
    """The factory session `name` of this workspace (watched), or None."""
    return next((w for w in watched(ws) if w["name"] == name), None) if isinstance(name, str) else None


def screens(names: list[str]) -> dict:
    """terminals.capture_many on the factory's own tmux socket (escaped HTML screens)."""
    from orch.dashboard import terminals
    return terminals.capture_many(names, run=_tmux)


class TmuxLauncher:
    def human_typed(self, name: str) -> bool:
        """Whether the human typed into this session from the browser within HUMAN_QUIET seconds."""
        return human_typed(name)

    def alive(self) -> set[str] | None:
        """Sessions whose pane still runs. Panes stay after their process exits (remain-on-exit, set at start), so
        the runner can read how a session ended (reap) before it ends the session."""
        try:
            r = _tmux(["list-panes", "-a", "-F", "#{session_name} #{pane_dead}"])
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
            r = _tmux(["display-message", "-p", "-t", f"={name}:", "#{pane_dead} #{pane_dead_status}"])
            dead, _, status = r.stdout.strip().partition(" ")
            if r.returncode != 0 or dead != "1":
                return None
            text = _tmux(["capture-pane", "-p", "-J", "-t", f"={name}:", "-S", "-200"]).stdout
            _tmux(["kill-session", "-t", f"={name}"])
        except (OSError, subprocess.TimeoutExpired, UsageError):
            return None
        return status, "\n".join(ln for ln in text.splitlines() if not ln.startswith("Pane is dead"))

    def capture(self, name: str) -> str | None:
        """The visible text of the session's pane (plain, no escapes), or None."""
        try:
            r = _tmux(["capture-pane", "-p", "-t", f"={name}:"])
        except (OSError, subprocess.TimeoutExpired, UsageError):
            return None
        return r.stdout if r.returncode == 0 else None

    def _clear_own(self, name: str, screen) -> bool:
        """Clear the input box with C-u only while `screen` (the pane as just read) shows the runner's own nudge text
        in it (never the human's or the agent's), then read it again: True when the box is empty now."""
        if human_typed(name) or factory_runner.leftover(screen) is None:
            return False
        _tmux(["send-keys", "-t", f"={name}:", "C-u"])
        _sleep(TYPE_POLL_SECONDS)
        return factory_runner.input_line(self.capture(name)) == ""

    def type(self, name: str, text: str):
        """Type one of the runner's built-in nudges into the pane: only onto an empty input box (a nudge an earlier
        attempt left there is cleared first), then read the pane again a few times over about a second (tmux redraws
        asynchronously) and press Enter only when the text sits in the input box itself and nothing Enter would
        answer instead is on screen (typed_ok; only the box and the busy markers count, not the status bar). True
        when Enter was pressed; "cleaned" when it did not land as it should and the box held only the runner's text,
        which was cleared again (C-u), so nothing is left behind; False otherwise. Never Enter on anything else, and
        nothing else is ever typed."""
        if not factory_runner.nudge_ok(text):
            raise UsageError("the runner types only its built-in nudges")
        screen = self.capture(name)
        line = factory_runner.input_line(screen)
        if line and not self._clear_own(name, screen):
            return False  # someone else's text, or our leftover that would not clear: touch nothing more
        if line is None:
            return False
        if _tmux(["send-keys", "-t", f"={name}:", "-l", "--", text]).returncode != 0:
            raise UsageError(f"could not type into {name}")
        screen = None
        for _ in range(TYPE_POLLS):
            _sleep(TYPE_POLL_SECONDS)
            if human_typed(name):
                return False  # the human started typing meanwhile: neither Enter nor C-u touches their input
            screen = self.capture(name)
            if factory_runner.typed_ok(screen, text):
                if human_typed(name):
                    return False
                return _tmux(["send-keys", "-t", f"={name}:", "Enter"]).returncode == 0
        return "cleaned" if self._clear_own(name, screen) else False

    def start(self, name: str, cwd: str, argv: list[str]) -> int:
        # argv already starts with `env -i ...`: the session's shell command holds nothing of the server's environment.
        # remain-on-exit is set (server-wide, in the same tmux call, before the session exists) so a session that ends
        # right away leaves its last screen and exit status for reap().
        r = _tmux(["start-server", ";", "set-option", "-g", "-w", "remain-on-exit", "on", ";",
                   "new-session", "-d", "-s", name, "-c", launch.tmux_arg(cwd), "-x", "160", "-y", "45",
                   launch.tmux_arg(shlex.join(argv))])
        if r.returncode != 0:
            raise UsageError(f"tmux could not start a session named {name}")
        r = _tmux(["display-message", "-p", "-t", f"={name}:", "#{pane_pid}"])
        return int(r.stdout.strip())  # ValueError (no pid) ends the session in the caller: nothing runs unbound

    def stop(self, name: str) -> None:
        if _tmux(["kill-session", "-t", f"={name}"]).returncode != 0:
            raise UsageError(f"{name} could not be ended (has it ended already?)")


_SAID: dict[str, str | None] = {}  # the last blocking reason logged, per workspace: said once, not every round


def run_once(ws, launcher=None) -> list[str]:
    """One runner round. When the runner cannot start anything (factory_runner.runner_blocker: a program, the user
    settings, readiness), that is said on the terminal once per change, and every view shows the same reason."""
    from orch.core import factory_sessions, permits
    on = permits.enabled(ws)
    key = str(ws.root)
    if not on and permits.config_enabled(ws):  # the config asks, nothing signed it: say how to migrate, start nothing
        if _SAID.get(key) != permits.UNSIGNED:
            _SAID[key] = permits.UNSIGNED
            log.warning("factory runner starts nothing: %s", permits.UNSIGNED)
    if not (on or factory_sessions.bindings(ws)):
        return []
    settings = launch.load_settings()
    why = factory_runner.program_blocker(settings) if on else None
    if why != _SAID.get(key):
        _SAID[key] = why
        if why:
            log.warning("factory runner starts nothing: %s", why)
    if not available():  # no tmux at a trusted path: nothing can start (the views say why)
        return []
    return factory_runner.tick(ws, HUMAN, launcher or TmuxLauncher(), settings=settings)


def configure_logging() -> None:
    """The runner's lines (sessions started, refused, stopped, nudged) reach the terminal that runs the dashboard: a
    handler on the `orch.factory` logger at INFO unless one is configured already. Nothing else changes."""
    if log.handlers:
        return
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("orch factory: %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False


def release_once(ws, run=None) -> list[str]:
    """One release round (phase 6, orch.core.factory_release): Ready Dark epics whose charter signs a release go
    through the human's recipe; then Dark epics whose charter signs `close` and whose every condition holds are closed
    by the charter (orch.core.factory_close). Needs no tmux; nothing unless the factory is on."""
    from orch.core import factory_close, factory_release, permits
    if not permits.enabled(ws):
        return []
    return factory_release.tick(ws, HUMAN, run) + factory_close.tick(ws, HUMAN)


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
    return factory_runner.sweep(ws, HUMAN, launcher or TmuxLauncher())


def shutdown(ws, launcher=None) -> list[str]:
    """Dashboard stop: every factory session stops and every binding ends (a child may start again next time)."""
    from orch.core import factory_sessions
    if not factory_sessions.bindings(ws):
        return []
    return factory_runner.sweep(ws, HUMAN, launcher or TmuxLauncher(), stop_all=True)


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
