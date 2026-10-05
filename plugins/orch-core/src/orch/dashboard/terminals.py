"""Mission Control's own terminals: agent sessions in orch's tmux server (`tmux -L orch`, issue #40).

The `tmux` launcher (launch.argv_for) starts an agent there; this module lists those sessions, takes a snapshot of a
screen (`capture-pane -e`) as escaped HTML, and sends keys (`send-keys`). No pty and no WebSocket: the browser gets
snapshots over server-sent events and posts keys back. Every call is an argv list to tmux, never a shell, and every
session name is validated and must belong to this workspace (its start folder is inside the workspace root) before
it reaches tmux. It is off until the human enables the `terminals` default addon in this workspace, and absent
without tmux on PATH: no menu item, no button, every /terminals route but the page itself answers 404. It also
answers only to a request from this machine that names a loopback host (local_request), whatever `orch serve` binds
to: a terminal is a shell as the user.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orch.dashboard.launch import tmux_arg
from orch.errors import UsageError, ValidationError

SOCKET = "orch"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
MAX_TEXT = 4096  # characters per text item of one keys POST
TAIL = 8  # lines a tile shows: the bottom of the screen, at a readable size

which = shutil.which  # module-level so tests can stand in for a missing or present tmux

# Keys a browser cannot type as text, by the names tmux's send-keys knows. C-a … C-z are allowed on top.
KEYS = frozenset({"Enter", "Escape", "Tab", "BTab", "BSpace", "DC", "Up", "Down", "Left", "Right", "Home", "End",
                  "PPage", "NPage", "Space"})
_CTRL = re.compile(r"C-[a-z]")


def tmux(args: list[str], timeout: float = 5) -> subprocess.CompletedProcess:
    """Run one tmux command against orch's server. Tests replace this."""
    return subprocess.run(["tmux", "-L", SOCKET, *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def available() -> bool:
    return which("tmux") is not None


ADDON = "terminals"
OFF = "Terminals is an addon: enable it in Workspace & addons"


def addon_on(ws) -> bool:
    """The `terminals` default addon is enabled (and trusted) in this workspace: it is the switch for all of this."""
    try:
        return ws.addons.get(ADDON) is not None
    except Exception:  # a broken addon registry never breaks a page
        return False


def local_request(request) -> bool:
    """The request comes from this machine and its Host is loopback, or from a paired device through the bridge
    (the remote gate holds that device to the route's scope). Never over the LAN option (the network, a phone), and
    never to a Host that only resolves to this machine (DNS rebinding). The answer is reach.reach."""
    from orch.dashboard.reach import reach
    return reach(request).kind != "refused"


LOCAL_ONLY = "Terminals open only on this machine: use the 127.0.0.1 link, not the network address"


def why_off(ws) -> str:
    """Why a Mission Control start was refused, for the message."""
    if not addon_on(ws):
        return OFF
    return LOCAL_ONLY if available() else "tmux is not installed"


def enabled(ws, request=None) -> bool:
    """Terminals are on: the addon is enabled here and tmux is installed; given a request, it is also a local one."""
    return addon_on(ws) and available() and (request is None or local_request(request))


SETTINGS = {"harness": "claude", "open_here": True}  # the addon's settings_schema defaults (addons/terminals)


def settings(root) -> dict:
    """The addon's settings for this workspace (Workspace & addons), over the defaults; never raises."""
    try:
        from orch.addons.userfiles import workspace_addons
        saved = workspace_addons(root).get(ADDON, {}).get("config", {}) or {}
    except Exception:
        saved = {}
    return {**SETTINGS, **{k: v for k, v in saved.items() if k in SETTINGS}}


@dataclass(frozen=True)
class Session:
    name: str
    path: str
    created: int
    activity: int
    cols: int
    rows: int
    pid: int = 0  # the pane's process: the agent itself (its shell execs it), for agentinfo


_FORMAT = ("#{session_name}\t#{session_path}\t#{session_created}\t#{session_activity}\t#{window_width}\t#{window_height}"
           "\t#{pane_pid}")


def sessions(ws) -> list[Session]:
    """This workspace's sessions, most recently active first. No server yet (or no tmux) means none."""
    if not available():
        return []
    try:
        r = tmux(["list-sessions", "-F", _FORMAT])
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    root = Path(ws.root).resolve()
    out = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 7 or not NAME.fullmatch(parts[0]):
            continue
        path = Path(parts[1]).resolve()
        if path != root and root not in path.parents:
            continue
        try:
            out.append(Session(parts[0], parts[1], int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]),
                               int(parts[6] or 0)))
        except ValueError:
            continue
    return sorted(out, key=lambda s: s.activity, reverse=True)


def find(ws, name: str) -> Session:
    """The session `name` of this workspace, or ValidationError (the routes answer 404)."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValidationError("not a terminal name")
    for s in sessions(ws):
        if s.name == name:
            return s
    raise ValidationError(f"no terminal named {name} in this workspace")


def _target(name: str) -> str:
    return f"={name}:"  # exact session name (no prefix match), its current window and pane


def free_name(ws, base: str) -> str:
    """`base`, or `base-2`, `base-3`, … if a session (of any workspace) already has that name."""
    if not NAME.fullmatch(base):
        raise ValidationError("not a terminal name")
    try:
        r = tmux(["list-sessions", "-F", "#{session_name}"])
        taken = set(r.stdout.split()) if r.returncode == 0 else set()
    except (OSError, subprocess.TimeoutExpired):
        taken = set()
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def capture(name: str) -> dict | None:
    """The visible screen as {"html", "cols", "rows"} (escaped HTML and the pane's size, so the browser can scale it
    to fit), or None when the session is gone. One tmux call: capture-pane, then the size on a last line."""
    try:
        r = tmux(["capture-pane", "-p", "-e", "-t", _target(name), ";",
                  "display-message", "-p", "-t", _target(name), "#{pane_width} #{pane_height}"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    text, _, size = r.stdout.rstrip("\n").rpartition("\n")
    return _screen(text, size)


def capture_many(names: list[str]) -> dict[str, dict | None]:
    """capture() for every session in `names` with ONE tmux call (the grid's tick): each screen comes after a header
    line that carries a fresh random marker, so text on a screen cannot pose as another session's header. A session
    the batch did not reach (it ended mid-way and tmux stopped there) is captured on its own, or None."""
    import secrets
    if not names:
        return {}
    mark = "\x1e" + secrets.token_hex(8)
    args: list[str] = []
    for name in names:
        if args:
            args.append(";")
        args += ["display-message", "-p", "-t", _target(name), f"{mark} #{{session_name}} #{{pane_width}} #{{pane_height}}",
                 ";", "capture-pane", "-p", "-e", "-t", _target(name)]
    try:
        r = tmux(args)
        out = r.stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    got: dict[str, dict | None] = {}
    parts = out.split(mark + " ")
    for part in parts[1:]:
        header, _, text = part.partition("\n")
        name, _, size = header.partition(" ")
        if name in names and name not in got:
            got[name] = _screen(text[:-1] if text.endswith("\n") else text, size)  # capture-pane's last newline
    return {n: got[n] if got.get(n) is not None else capture(n) for n in names}


def _screen(text: str, size: str) -> dict | None:
    try:
        cols, rows = (int(v) for v in size.split())
    except ValueError:
        return None
    lines = text.split("\n")
    while lines and not _OTHER_ESC.sub("", _SGR.sub("", lines[-1])).strip():
        lines.pop()  # blank rows under the last output: a tile shows the last lines that say something
    # html: the whole window, row for row ("Whole pane"); trim: without the blank rows under the last output (the
    # readable view follows its bottom); tail: the last TAIL of those lines (a tile)
    return {"html": ansi_to_html(text), "trim": ansi_to_html("\n".join(lines)), "tail": ansi_to_html("\n".join(lines[-TAIL:])),
            "cols": cols, "rows": rows}


def send(name: str, seq: list) -> None:
    """Send `seq`, a list of {"text": str} (typed literally) and {"key": name} items, in order."""
    if not isinstance(seq, list) or len(seq) > 256:
        raise ValidationError("keys must be a list of at most 256 items")
    calls = []
    for item in seq:
        if not isinstance(item, dict) or len(item) != 1:
            raise ValidationError("each item is {\"text\": …} or {\"key\": …}")
        if "text" in item:
            text = item["text"]
            if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
                raise ValidationError(f"text must be 1 to {MAX_TEXT} characters")
            if any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValidationError("text holds a control character; send it as a key")
            calls.append(["send-keys", "-t", _target(name), "-l", "--", tmux_arg(text)])
        elif "key" in item:
            key = item["key"]
            if not isinstance(key, str) or not (key in KEYS or _CTRL.fullmatch(key)):
                raise ValidationError(f"unknown key {key!r}")
            calls.append(["send-keys", "-t", _target(name), key])
        else:
            raise ValidationError("each item is {\"text\": …} or {\"key\": …}")
    for args in calls:
        if tmux(args).returncode != 0:
            raise UsageError(f"{name} did not take the keys (has it ended?)")


def resize(name: str, cols: int, rows: int) -> None:
    cols, rows = max(20, min(int(cols), 400)), max(5, min(int(rows), 200))
    tmux(["resize-window", "-t", _target(name), "-x", str(cols), "-y", str(rows)])


def end(name: str) -> None:
    if tmux(["kill-session", "-t", f"={name}"]).returncode != 0:
        raise UsageError(f"{name} could not be ended (has it ended already?)")


# ---- ANSI (SGR) to HTML -------------------------------------------------------------------------------------------

_BASE = ("#1d2025", "#ff6b6b", "#5be3b8", "#f5b544", "#6e9eff", "#c9a4ff", "#56d4e8", "#d6dae0",
         "#6b7480", "#ff8a80", "#a9f7c0", "#ffd479", "#9cc0ff", "#e0c4ff", "#8ee8f5", "#ffffff")
_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_OTHER_ESC = re.compile(r"\x1b(?:\[[0-9;:?<>=]*[ -/]*[@-ln-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][0-9A-Za-z]|[=>78MDEc])")


def _c256(n: int) -> str:
    if n < 16:
        return _BASE[n]
    if n < 232:
        n -= 16
        steps = (0, 95, 135, 175, 215, 255)
        return "#%02x%02x%02x" % (steps[n // 36], steps[n // 6 % 6], steps[n % 6])
    v = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def _apply(codes: list[int], st: dict) -> None:
    i = 0
    while i < len(codes):
        c = codes[i]
        if c == 0:
            st.clear()
        elif c in (1, 2, 3, 4, 7, 9):
            st[c] = True
        elif c == 22:
            st.pop(1, None)
            st.pop(2, None)
        elif c in (23, 24, 27, 29):
            st.pop(c - 20, None)
        elif 30 <= c <= 37 or 90 <= c <= 97:
            st["fg"] = _BASE[c - 30 if c < 90 else c - 82]
        elif 40 <= c <= 47 or 100 <= c <= 107:
            st["bg"] = _BASE[c - 40 if c < 100 else c - 92]
        elif c == 39:
            st.pop("fg", None)
        elif c == 49:
            st.pop("bg", None)
        elif c in (38, 48) and i + 1 < len(codes):
            slot = "fg" if c == 38 else "bg"
            if codes[i + 1] == 5 and i + 2 < len(codes):
                st[slot] = _c256(codes[i + 2] % 256)
                i += 2
            elif codes[i + 1] == 2 and i + 4 < len(codes):
                st[slot] = "#%02x%02x%02x" % tuple(max(0, min(v, 255)) for v in codes[i + 2:i + 5])
                i += 4
        i += 1


def _style(st: dict) -> str:
    fg, bg = st.get("fg"), st.get("bg")
    if st.get(7):
        fg, bg = bg or "#15171a", fg or "#d6dae0"
    parts = []
    if fg:
        parts.append(f"color:{fg}")
    if bg:
        parts.append(f"background:{bg}")
    if st.get(1):
        parts.append("font-weight:700")
    if st.get(2):
        parts.append("opacity:.6")
    if st.get(3):
        parts.append("font-style:italic")
    if st.get(4) or st.get(9):
        parts.append("text-decoration:" + " ".join(d for k, d in ((4, "underline"), (9, "line-through")) if st.get(k)))
    return ";".join(parts)


def ansi_to_html(text: str) -> str:
    """Terminal output with SGR colours as <span style> runs; every character of the text itself is escaped, and
    every other escape sequence (cursor moves, titles, charset switches) is dropped."""
    text = _OTHER_ESC.sub("", text)
    out, st, pos = [], {}, 0

    def emit(chunk: str) -> None:
        chunk = chunk.replace("\x1b", "")
        if not chunk:
            return
        style = _style(st)
        esc = html.escape(chunk, quote=False)
        out.append(f'<span style="{style}">{esc}</span>' if style else esc)

    for m in _SGR.finditer(text):
        emit(text[pos:m.start()])
        raw = m.group(1).replace(":", ";")
        codes = [int(p) if p.isdigit() else 0 for p in raw.split(";")] if raw else [0]
        _apply(codes, st)
        pos = m.end()
    emit(text[pos:])
    return "".join(out)
