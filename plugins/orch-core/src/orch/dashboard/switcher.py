"""Multi-workspace switcher (spec §4.2): no cross-workspace server, just a per-user file,
`~/.config/orch/workspaces.json` (honouring `ORCH_STATE_DIR` the same way `launch.config_dir()`
does, so it lives next to `launch.json`), that each running workspace updates with its own path,
name and port, and each workspace's own `state_dir/needs-count` file that the others read.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import socket
import sys
from pathlib import Path

from orch.clock import stamp
from orch.core.fsutil import atomic_write_text, read_regular_file
from orch.dashboard.launch import config_dir

log = logging.getLogger("orch.dashboard")
_COUNT_MAX_BYTES = 16


def _workspaces_path() -> Path:
    return config_dir() / "workspaces.json"


_NAME_MAX = 80


def _load() -> dict:
    """The raw `workspaces.json`, defensively: a missing, unreadable, non-JSON or non-object
    file (hand-edited, truncated, or written by some future/older version) is treated as empty,
    never raised, so a broken file only loses the switcher list, never breaks the dashboard or
    `register()`."""
    try:
        data = json.loads(_workspaces_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_SCAN = 50


def workspace_id(ws) -> str:
    """A stable, opaque id for this workspace: random, URL-safe, stored in the workspace's own state
    directory (so a rename or move of the directory keeps it) and never derived from its path or name.
    It is an unauthenticated routing label kept in an agent-writable directory: it must never authorize
    anything. Created once; a missing or malformed file gets a fresh id. Two starters at once agree on one id."""
    path = ws.state_dir / "workspace-id"
    for _ in range(3):
        raw = read_regular_file(path, 128)
        found = raw.decode("utf-8", "replace").strip() if raw is not None else ""
        if _ID_RE.fullmatch(found):
            return found
        new = secrets.token_urlsafe(16)
        tmp = path.with_name(f"workspace-id.{os.getpid()}.{secrets.token_hex(4)}")
        atomic_write_text(tmp, new + "\n")
        try:
            if raw is None:
                os.link(tmp, path)  # fails when another starter won: read theirs
            else:
                os.replace(tmp, path)  # replace a malformed one
                return new
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    raise OSError(errno.EEXIST, "could not settle a workspace id")


def pid_alive(pid) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":  # os.kill would send a console event there; the pid is unknown, the entry stays
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except OSError:  # PermissionError: it exists, it is just not ours
        return True
    return True


def remembered_port(ws) -> int | None:
    """The port this workspace last ran on, if the file says so validly."""
    try:
        entry = _load().get(str(ws.root.resolve()))
        return _valid_port(entry.get("last_port")) if isinstance(entry, dict) else None
    except Exception:
        return None


def candidate_ports(remembered: int | None, configured: int) -> list[int]:
    """Remembered first, then the configured port, then up to +50 above it."""
    out: list[int] = []
    for p in [remembered, *range(configured, configured + _SCAN + 1)]:
        if p is not None and 1 <= p <= 65535 and p not in out:
            out.append(p)
    return out


def _someone_listens(port: int) -> bool:
    """A quick connect on loopback (v4, and v6 when there is one). With SO_REUSEADDR some systems let a
    wildcard listener and a loopback listener share a number, so a bind alone is not proof the port is free."""
    for addr, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((addr, port)) == 0:
                    return True
        except (OSError, OverflowError, ValueError):
            continue
    return False


def listen_first_free(host: str, ports: list[int]) -> tuple[socket.socket, int]:
    """Bind (and listen on) the first free port of `ports` and return that very socket: the bind is
    the claim, so two starts at the same instant cannot both get one port. Raises OSError if none."""
    last: OSError | None = None
    for p in ports:
        if _someone_listens(p):  # before our own bind: afterwards a connect would reach us
            last = OSError(errno.EADDRINUSE, "port in use")
            continue
        try:
            family = socket.getaddrinfo(host, p, type=socket.SOCK_STREAM)[0][0]
            sock = socket.socket(family, socket.SOCK_STREAM)
        except (OSError, OverflowError, ValueError, IndexError) as exc:
            last = exc if isinstance(exc, OSError) else OSError(errno.EINVAL, str(exc))
            continue
        try:
            if sys.platform != "win32":  # on Windows this option would let two binders share a port
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, p))
            sock.listen(128)
            return sock, p
        except (OSError, OverflowError, ValueError) as exc:
            sock.close()
            last = exc if isinstance(exc, OSError) else OSError(errno.EINVAL, str(exc))
    raise last or OSError(errno.EADDRNOTAVAIL, "no port to try")


def register(ws, port: int) -> None:
    """Record this workspace in `workspaces.json`, keyed by its real path, so other running
    workspaces' switchers can link to it; merges into the entry, so the addons section survives.
    Stores `state_dir` too, so `others()` never has to open another workspace to find its
    needs-count file."""
    from orch.addons.userfiles import update_json

    key = str(ws.root.resolve())

    def mutate(data: dict) -> None:
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        entry.update({"path": key, "name": ws.config.get("customer") or ws.root.name, "last_port": port,
                      "pid": os.getpid(), "workspace_id": workspace_id(ws), "state_dir": str(ws.state_dir), "updated": stamp()})
        data[key] = entry

    update_json(_workspaces_path(), mutate)


def _valid_port(value) -> int | None:
    """A real TCP port: an int (not a bool, which is an int subclass) in 1..65535. Anything else
    (a string like "8766@evil.example.com", a float, None, ...) is not a port, so the URL is
    never built from it."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 65535 else None


def _valid_str(value) -> str | None:
    return value if isinstance(value, str) else None


def _needs_for(state_dir: str | None) -> int | None:
    """Another workspace's needs-you count, or None. The path comes from the user-level
    workspaces.json, so the file may be anything: it is opened non-blocking (a FIFO never hangs
    the page), must be a regular file of at most 16 bytes, and at most 16 bytes are read."""
    if not state_dir:
        return None
    raw = read_regular_file(Path(state_dir, "needs-count"), _COUNT_MAX_BYTES)
    try:
        return int(raw.decode("utf-8").strip()) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        return None


def others(ws) -> list[dict]:
    """Other registered workspaces with a valid `last_port` and a path that still exists, sorted
    by name, each as {name, url, needs}. Runs on every page render, so it must never raise: a
    malformed `workspaces.json` (wrong top-level type, a non-dict entry, or a field of the wrong
    type) yields fewer or no entries, never a 500."""
    try:
        key = str(ws.root.resolve())
        data = _load()
    except Exception:
        return []
    out = []
    for path, entry in data.items():
        try:
            if path == key or not isinstance(entry, dict):
                continue
            port = _valid_port(entry.get("last_port"))
            if port is None:
                continue
            if "pid" in entry and not pid_alive(entry["pid"]):
                continue  # not running any more: its port may belong to someone else by now
            entry_path = _valid_str(entry.get("path"))
            if not entry_path or not Path(entry_path).exists():
                continue
            name = _valid_str(entry.get("name"))
            if not name or not name.strip():
                continue
            state_dir = _valid_str(entry.get("state_dir"))
            out.append({
                "name": name[:_NAME_MAX],
                "url": f"http://127.0.0.1:{port}/",
                "needs": _needs_for(state_dir),
            })
        except Exception:  # one malformed entry must never take down the whole switcher
            continue
    out.sort(key=lambda e: e["name"])
    return out


def write_needs(ws, n: int) -> None:
    """Write the needs-you count for this workspace atomically, and only when it changed, so
    other workspaces' switchers can show it without a cross-workspace server and without a write
    on every request that did not change anything. Runs on every page render: an OSError (a
    read-only or full state dir, a directory in the way) is logged and swallowed, never raised."""
    path = ws.state_dir / "needs-count"
    try:
        try:
            current = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            current = None
        text = str(n)
        if current == text:
            return
        atomic_write_text(path, text)
    except OSError as exc:
        log.warning("could not write %s for the workspace switcher: %s", path, exc)


def refresh_needs(ws) -> None:
    """Recompute this workspace's needs-you count and write it, so other workspaces' switchers
    stay current without an open tab here. For the background loop: never raises."""
    from orch.core import query

    try:
        write_needs(ws, query.counts(query.waiting(ws))["blocking"])
    except Exception as exc:  # a broken ticket tree must not stop the loop
        log.warning("could not refresh the needs-you count: %s", exc)
