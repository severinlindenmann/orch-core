"""Multi-workspace switcher (spec §4.2, #167): no cross-workspace server, just a per-user file,
`~/.config/orch/workspaces.json` (honouring `ORCH_STATE_DIR` the same way `launch.config_dir()`
does, so it lives next to `launch.json`), that each running workspace updates with its own path,
name, port and pid. Each dashboard answers `GET /__orch/status` on loopback; a switcher lists another
workspace only when that answer comes from orch-core and names the workspace it expects. Off unless
the human turns it on (userfiles.workspace_switcher).
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
import threading
import time
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
    `state_dir` is kept for older versions, which read the needs-count file from it."""
    from orch.addons.userfiles import update_json

    key = str(ws.root.resolve())

    def mutate(data: dict) -> None:
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        entry.update({"path": key, "name": display_name(ws), "last_port": port,
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
    """The needs-you count in `state_dir/needs-count`, or None. The file may be anything: it is opened
    non-blocking (a FIFO never hangs the status endpoint), must be a regular file of at most 16 bytes,
    and at most 16 bytes are read."""
    if not state_dir:
        return None
    raw = read_regular_file(Path(state_dir, "needs-count"), _COUNT_MAX_BYTES)
    try:
        return int(raw.decode("utf-8").strip()) if raw is not None else None
    except (UnicodeDecodeError, ValueError):
        return None


SERVICE = "orch-core-dashboard"
STATUS_PATH = "/__orch/status"
STATUS_SCHEMA = 1
PROBE_TIMEOUT = 0.2
REFRESH_SECONDS = 5.0
_PROBE_MAX_BYTES = 8192
GONE = "gone"  # probe verdict: nothing listens there, or something that is not an orch dashboard answers


def display_name(ws) -> str:
    return ws.config.get("customer") or ws.root.name


def status(ws, *, started: str, addons=()) -> dict:
    """What `GET /__orch/status` answers: who serves this port. No ticket content and no paths."""
    from orch import __version__

    return {"service": SERVICE, "schema": STATUS_SCHEMA, "orch_version": __version__,
            "workspace_id": workspace_id(ws), "name": display_name(ws), "pid": os.getpid(), "started": started,
            "addons": [{"name": la.name, "version": la.manifest.version} for la in addons],
            "needs": _needs_for(str(ws.state_dir))}


def probe(port: int, timeout: float = PROBE_TIMEOUT):
    """The status JSON of whatever serves 127.0.0.1:`port`; GONE when nothing listens or something else answers;
    None when it did not answer in time (a busy dashboard is not a stale one)."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", STATUS_PATH, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(_PROBE_MAX_BYTES + 1)
    except TimeoutError:
        return None
    except (OSError, http.client.HTTPException, ValueError):
        return GONE
    finally:
        conn.close()
    if resp.status != 200 or len(body) > _PROBE_MAX_BYTES:
        return GONE
    try:
        data = json.loads(body)
    except ValueError:
        return GONE
    return data if isinstance(data, dict) else GONE


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def scan(ws) -> list[dict]:
    """Other workspaces whose dashboard runs now, sorted by name, each as {name, url, needs}. Blocking: it probes
    each candidate's port. An entry counts only with a live pid and a status answer from orch-core for its own
    workspace_id; one that fails for sure loses its pid in workspaces.json. Never raises."""
    try:
        key = str(ws.root.resolve())
        data = _load()
    except Exception:
        return []
    out, stale = [], []
    for path, entry in data.items():
        try:
            if path == key or not isinstance(entry, dict) or "pid" not in entry:
                continue  # no pid: an older version's entry, or a dashboard that shut down cleanly
            port = _valid_port(entry.get("last_port"))
            entry_path = _valid_str(entry.get("path"))
            name = _valid_str(entry.get("name"))
            wid = _valid_str(entry.get("workspace_id"))
            if port is None or not entry_path or not name or not name.strip() or not wid:
                continue
            if not Path(entry_path).exists():
                continue
            if not pid_alive(entry["pid"]):
                stale.append((path, entry["pid"], port))
                continue
            got = probe(port)
            if got is None:
                continue
            if got is GONE or got.get("service") != SERVICE or got.get("workspace_id") != wid:
                stale.append((path, entry["pid"], port))  # pid reused, or the port belongs to someone else now
                continue
            out.append({"name": name[:_NAME_MAX], "url": f"http://127.0.0.1:{port}/", "needs": _count(got.get("needs"))})
        except Exception:  # one malformed entry must never take down the whole switcher
            continue
    if stale:
        _prune(stale)
    out.sort(key=lambda e: e["name"])
    return out


def _prune(stale: list[tuple]) -> None:
    """Clear the pid of entries found stale, unless they re-registered meanwhile. The entry itself stays: it also
    holds that workspace's addon switches and dashboard settings."""
    from orch.addons.userfiles import update_json

    def mutate(data: dict) -> None:
        for path, pid, port in stale:
            entry = data.get(path)
            if isinstance(entry, dict) and entry.get("pid") == pid and entry.get("last_port") == port:
                del entry["pid"]

    try:
        update_json(_workspaces_path(), mutate)
    except Exception as exc:
        log.warning("could not prune stale entries from workspaces.json: %s", exc)


_lock = threading.Lock()
_cache: dict[str, tuple[float, list]] = {}
_running: set[str] = set()


def others(ws) -> list[dict]:
    """The switcher list for a page render: the last scan, never waiting on one. A scan older than REFRESH_SECONDS
    (or none yet) starts a fresh one in a background thread. Never raises."""
    try:
        key = str(ws.root.resolve())
        with _lock:
            at, entries = _cache.get(key, (None, []))
            if (at is None or time.monotonic() - at >= REFRESH_SECONDS) and key not in _running:
                _running.add(key)
                try:
                    threading.Thread(target=_refresh, args=(ws, key), name="orch-switcher", daemon=True).start()
                except RuntimeError:
                    _running.discard(key)
        return list(entries)
    except Exception:
        return []


def _refresh(ws, key: str) -> None:
    entries: list = []
    try:
        entries = scan(ws)
    except Exception as exc:  # scan never raises; this only keeps the thread quiet if it ever does
        log.warning("workspace switcher scan failed: %s", exc)
    finally:
        with _lock:
            _cache[key] = (time.monotonic(), entries)
            _running.discard(key)


def unregister(ws) -> None:
    """On a clean shutdown: clear our pid from our entry, so no switcher links to this port any more."""
    from orch.addons.userfiles import update_json

    key, me = str(ws.root.resolve()), os.getpid()

    def mutate(data: dict) -> None:
        entry = data.get(key)
        if isinstance(entry, dict) and entry.get("pid") == me:
            del entry["pid"]

    update_json(_workspaces_path(), mutate)


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
