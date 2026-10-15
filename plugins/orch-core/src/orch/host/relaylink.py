"""The host's side of a relay link (orch-v2-host §11A.4, §11A.6, §11A.7; H12a): the relay-linked workspace cap, the
link reservation's critical section and the persistent stop of ``orch host relay stop``.

* **The cap.** v1.7 serves at most :data:`MAX_LINKED` relay-linked workspaces per host (4 relay threads and
  connections each, ≤ 36 MiB each, §11A.7). A workspace is relay-linked while ``hosts/<ws>/relay/link.json`` exists
  (state ``linking`` or ``linked``; a dangling link or an entry that cannot be inspected counts too). ``relay.link``
  above the cap is refused with ``host.limit``.
* **The reservation.** The only writer of ``link.json`` is ``store.relay_link(link, guard=lambda:
  relaylink.link_reservation(service))`` (H12b), state ``linking``, before any network write; the only rewrite is
  ``store.relay_mark_linked(expect)`` (``linking`` to ``linked``). :func:`link_reservation` is the guard: under the
  store lock and a host-wide lock it refuses a workspace whose history or durable mark holds ``auth: "convenience"``
  or whose convenience flag is on (``sign.convenience_readers``, §11A.6, strict in v1.7) and a link above the cap;
  the store then refuses an existing link (``relay.link_exists``) and writes inside both locks.
* **The stop.** ``hosts/<ws>/relay/stopped.json`` (0600) keeps the relay worker stopped across host restarts, the
  operator procedure after a state restore (§11A.4 "Scope of exactly-once"). :func:`may_start` is the check the relay
  worker's start (R5) must pass: linked, and not stopped. A marker that cannot be read safely counts as stopped.
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .custody import ensure_private_dir, read_json, unsafe, write_private
from .errors import HostError

__all__ = [
    "MAX_LINKED",
    "clear_stopped",
    "is_stopped",
    "link_path",
    "link_reservation",
    "linked_workspaces",
    "mark_stopped",
    "may_start",
    "relay_dir",
    "stopped_path",
]

#: relay-linked workspaces per host (decided by orch-52, 13 Oct 2026)
MAX_LINKED = 8


def relay_dir(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "relay"


def link_path(state_dir: Path, workspace_id: str) -> Path:
    return relay_dir(state_dir, workspace_id) / "link.json"


def stopped_path(state_dir: Path, workspace_id: str) -> Path:
    return relay_dir(state_dir, workspace_id) / "stopped.json"


def linked_workspaces(state_dir: Path) -> list[str]:
    """The workspaces with a ``relay/link.json`` under ``<state>/hosts/`` (registered or not). An entry that cannot be
    inspected counts as linked; a ``hosts/`` directory that cannot be listed refuses (fail closed)."""
    hosts = Path(state_dir) / "hosts"
    try:
        names = sorted(os.listdir(hosts))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise unsafe(f"{hosts}: {exc.strerror or exc}") from None
    out = []
    for name in names:
        try:
            os.lstat(link_path(state_dir, name))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            pass  # cannot tell: counted
        out.append(name)
    return out


@contextlib.contextmanager
def link_reservation(service: Any) -> Iterator[None]:
    """The guard of ``store.relay_link`` (§11A.6), the only writer of ``link.json``:
    ``store.relay_link(link, guard=lambda: relaylink.link_reservation(service))``. Holds the store lock (re-entrant)
    and a host-wide lock (two workspaces linking at once cannot both take the last slot), then refuses with
    ``sign.convenience_readers`` (convenience history, mark or flag) and ``host.limit`` (the cap). An existing link is
    the store's refusal (``relay.link_exists``), so the guard has no relink case."""
    from .convenience import _flock, refuse_reader_op

    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    with store.locked(), _flock(service.state_dir, "relay-link.lock", service.host_uid):
        refuse_reader_op(service, "relay.link", history=True)
        linked = linked_workspaces(service.state_dir)
        if len(linked) >= MAX_LINKED:
            raise HostError(
                "host.limit",
                f"this host already serves {len(linked)} relay-linked workspaces (at most {MAX_LINKED})",
                hint="unlink another relay-linked workspace first, or serve this one from another host",
            )
        yield


def _private_relay_dir(state_dir: Path, workspace_id: str, host_uid: int) -> Path:
    hosts = Path(state_dir) / "hosts"
    ensure_private_dir(hosts, host_uid)
    ensure_private_dir(hosts / workspace_id, host_uid)
    return ensure_private_dir(relay_dir(state_dir, workspace_id), host_uid)


def mark_stopped(state_dir: Path, workspace_id: str, host_uid: int, *, clock: Any = time.time) -> None:
    """Write the stop marker (0600): the relay worker stays stopped until :func:`clear_stopped`."""
    _private_relay_dir(state_dir, workspace_id, host_uid)
    write_private(stopped_path(state_dir, workspace_id), {"v": 1, "at_ms": int(clock() * 1000)})


def clear_stopped(state_dir: Path, workspace_id: str, host_uid: int) -> None:
    if not os.path.lexists(relay_dir(state_dir, workspace_id)):
        return
    _private_relay_dir(state_dir, workspace_id, host_uid)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(stopped_path(state_dir, workspace_id))


def is_stopped(state_dir: Path, workspace_id: str, host_uid: int) -> bool:
    """True when the stop marker exists, whatever its content (a link or JSON ``null`` included), when the relay
    directory is not a real directory, or when either cannot be inspected (fail closed)."""
    rdir = relay_dir(state_dir, workspace_id)
    try:
        info = os.lstat(rdir)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISDIR(info.st_mode):
        return True  # a symlinked or replaced relay directory: never "not stopped"
    try:
        os.lstat(stopped_path(state_dir, workspace_id))
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def may_start(state_dir: Path, workspace_id: str, host_uid: int) -> bool:
    """The relay worker may start (R5): the relay directory is a private directory of the host UID with safe
    ancestors, ``link.json`` is a private JSON object of the host UID and the worker is not stopped. Anything else,
    including any doubt, is ``False``."""
    if is_stopped(state_dir, workspace_id, host_uid):
        return False
    try:
        ensure_private_dir(relay_dir(state_dir, workspace_id), host_uid, create=False)
        link = read_json(link_path(state_dir, workspace_id), host_uid)
    except (HostError, OSError):
        return False
    return isinstance(link, dict)
