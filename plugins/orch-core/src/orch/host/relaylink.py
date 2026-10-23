"""The host's side of a relay link (orch-v2-host §11A.4, §11A.6, §11A.7; H12a): the relay-linked workspace cap, the
link reservation's critical section and the persistent stop of ``orch host relay stop``.

* **The cap.** v1.7 serves at most :data:`MAX_LINKED` relay-linked workspaces per host (4 relay threads and
  connections each, ≤ 36 MiB each, §11A.7). A workspace is relay-linked while ``hosts/<ws>/relay/link.json`` exists
  (state ``linking`` or ``linked``; a dangling link or an entry that cannot be inspected counts too). ``relay.link``
  above the cap is refused with ``host.limit``.
* **The reservation.** The only writer of ``link.json`` is ``store.relay_link(link, guard=lambda:
  relaylink.link_reservation(service, relay_level=N))`` (H12b), state ``linking``, before any network write; the only
  rewrite is ``store.relay_mark_linked(expect)`` (``linking`` to ``linked``). :func:`link_reservation` is the guard:
  under the store lock and a host-wide lock it refuses a relay whose level (``GET /v2/relay-info`` at link step 1,
  H12d-3) is below ``required_reader`` (``sign.convenience_readers`` reason ``relay_too_old``; the relaxed rule of
  §0.8.6 item 9.4: a level-1 relay is linked with convenience history and while the flag is on) and a link above the
  cap, and stores the level with the link (``relay/reader.json``); the store then refuses an existing link
  (``relay.link_exists``) and writes inside both locks.
* **The level at every R1 login** (H12d-3, 9.8): :func:`observe_relay_level` is the session's ``relay_info`` hook,
  bound to the session's link origin. Under ``relay-link.lock`` it stores the level of that link only and alarms
  ``relay_reader_old`` when it is below ``required_reader``; R3 then holds every restore whose evidence carries a
  convenience event (:func:`orch.ops.readers.restore_held`).
* **The stop.** ``hosts/<ws>/relay/stopped.json`` (0600) keeps the relay worker stopped across host restarts, the
  operator procedure after a state restore (§11A.4 "Scope of exactly-once"). :func:`may_start` is the check the relay
  worker's start (R5) must pass: linked, not stopped, and the R6 gate open (:data:`orch.relay.worker.R6_READY`,
  §11A.1). :func:`start_blocker` names what blocks it (``host.status.relay``). A marker that cannot be read safely
  counts as stopped.
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
    "observe_relay_level",
    "relay_dir",
    "start_blocker",
    "unlink_intent_path",
    "unlink_pending",
    "mark_unlink_intent",
    "note_alarm",
    "read_unlink_intent",
    "clear_unlink_intent",
    "stopped_path",
]

#: relay-linked workspaces per host (decided by orch-52, 13 Oct 2026)
MAX_LINKED = 8


def relay_dir(state_dir: Path, workspace_id: str) -> Path:
    return Path(state_dir) / "hosts" / workspace_id / "relay"


def link_path(state_dir: Path, workspace_id: str) -> Path:
    return relay_dir(state_dir, workspace_id) / "link.json"


def local_epochs_path(state_dir: Path, workspace_id: str) -> Path:
    """The mark ``MembershipKeys.advance_local`` writes (V3 C2b): epochs made for the hub, unknown to any relay."""
    from orch.relay.r6.keystore import LOCAL_MARK

    return Path(os.path.realpath(state_dir)) / "hosts" / workspace_id / "membership" / LOCAL_MARK


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
def link_reservation(
    service: Any, *, relay_level: int | None = None, clock: Any = time.time, origin: str | None = None
) -> Iterator[None]:
    """The guard of ``store.relay_link`` (§11A.6), the only writer of ``link.json``:
    ``store.relay_link(link, guard=lambda: relaylink.link_reservation(service, relay_level=info.reader))``, with the
    level R5 read from ``GET /v2/relay-info`` just before (:func:`orch.relay.session.fetch_relay_info`; ``None``: not
    read, counts as 0). Holds the store lock (re-entrant) and a host-wide lock (two workspaces linking at once cannot
    both take the last slot), then refuses with ``sign.convenience_readers`` (``relay_too_old``: the level below
    ``required_reader``, from the flag, the mark or the merged history) and ``host.limit`` (the cap). While no
    ``link.json`` exists it stores the level (or clears a stale one when ``None``), so a refused relink never changes
    the level of the existing link. An existing link is the store's refusal (``relay.link_exists``), so the guard has
    no relink case."""
    from orch.store.readerrecs import clear_relay_level, write_relay_level

    from .convenience import _flock, refuse_reader_op
    from .devmode import refuse_link

    refuse_link(service, origin)  # §11A.17.3 F5, F2: dev_origin on a dev host, sandbox_workspace on a normal one
    store = service.store
    if store is None:
        raise HostError("workspace.unavailable", "the workspace is not open")
    with store.locked(), _flock(service.state_dir, "relay-link.lock", service.host_uid):
        if unlink_pending(service.state_dir, service.workspace_id):  # round 5 L1: finish the unlink first
            raise HostError(
                "lock.busy",
                "unlink_pending: an unlink of this workspace has not finished (it finishes at the next open)"[:118],
            )
        if os.path.lexists(local_epochs_path(service.state_dir, service.workspace_id)):
            raise HostError(
                "relay.local_epochs",
                "this workspace's keys were created for the hub; linking a relay comes with H5"[:118],
            )
        refuse_reader_op(service, "relay.link", history=True, relay_level=relay_level)
        linked = linked_workspaces(service.state_dir)
        if len(linked) >= MAX_LINKED:
            raise HostError(
                "host.limit",
                f"this host already serves {len(linked)} relay-linked workspaces (at most {MAX_LINKED})",
                hint="unlink another relay-linked workspace first, or serve this one from another host",
            )
        if not os.path.lexists(link_path(service.state_dir, service.workspace_id)):
            try:
                if relay_level is None:
                    clear_relay_level(service.state_dir, service.workspace_id)
                else:
                    write_relay_level(
                        service.state_dir, service.workspace_id, service.host_uid, relay_level, int(clock() * 1000)
                    )
            except HostError:
                raise
            except Exception as exc:  # noqa: BLE001 - the level must be stored with the link, or no link
                raise unsafe(f"the relay's reader level could not be stored ({type(exc).__name__})") from None
        yield


def observe_relay_level(
    state_dir: Path,
    workspace_id: str,
    host_uid: int,
    level: int,
    alarm: Any,
    *,
    origin: str,
    clock: Any = time.time,
    gated: Any = None,
) -> bool:
    """The R1 login hook (9.8), bound to its link (#555 review F2, DECIDED): R5 builds it per session with that
    session's link origin, ``relay_info=lambda lv: observe_relay_level(state, ws, uid, lv, alarm, origin=origin)``.
    Under the host-wide ``relay-link.lock`` (the link reservation's lock), it stores ``level`` only while
    ``link.json`` is still that link (a private JSON object naming ``origin``); a stale login of a former relay after
    a relink writes nothing, and an unlinked relay keeps its last level. It alarms ``relay_reader_old`` when the level
    is below ``required_reader`` (the flag or the durable mark; no history scan and no store lock: it runs on a relay
    thread under the session's login lock). Returns whether a stored level is below the required one. A busy lock or
    a level that cannot be stored keeps the stored one."""
    from orch.ops.readers import convenience_seen
    from orch.store.readerrecs import write_relay_level

    from .convenience import _flock, enabled

    try:
        with _flock(state_dir, "relay-link.lock", host_uid):
            link = read_json(link_path(state_dir, workspace_id), host_uid)
            if not isinstance(link, dict) or link.get("origin") != origin:
                return False
            # the flag or the Simulator dev gate (``gated``: orch.host.devmode.gated of the service, §11A.17.4; the
            # same rule as convenience.flag_or_gate), or the durable mark
            dev = gated is not None and gated() is True
            on = enabled(state_dir, workspace_id, host_uid) or dev or convenience_seen(state_dir, workspace_id)
            required = 1 if on else 0
            write_relay_level(state_dir, workspace_id, host_uid, level, int(clock() * 1000))
    except Exception as exc:  # noqa: BLE001 - the stored level stays; say so
        alarm("relay_reader_old", f"the relay's reader level {level} was not stored ({type(exc).__name__})")
        return False
    if level < required:
        alarm("relay_reader_old", f"the relay declares reader level {level}, this workspace requires {required}")
        return True
    return False


def _private_relay_dir(state_dir: Path, workspace_id: str, host_uid: int) -> Path:
    hosts = Path(state_dir) / "hosts"
    ensure_private_dir(hosts, host_uid)
    ensure_private_dir(hosts / workspace_id, host_uid)
    return ensure_private_dir(relay_dir(state_dir, workspace_id), host_uid)


def unlink_intent_path(state_dir: Path, workspace_id: str) -> Path:
    return relay_dir(state_dir, workspace_id) / "unlink-intent.json"


def mark_unlink_intent(
    state_dir: Path, workspace_id: str, host_uid: int, *, linked_at: int | None, clock: Any = time.time
) -> None:
    """The durable intent of ``orch host relay unlink`` (0600), written before anything else of the unlink: until
    the rename is done and the intent cleared, no worker starts (:func:`start_blocker`), ``relay start`` and a new
    reservation are refused, and the next open finishes the rename (R5 round 3, D-1). It is bound to the link it was
    made for (``linked_at``, round 5 L1): an intent for another link is stale and never acted on."""
    _private_relay_dir(state_dir, workspace_id, host_uid)
    write_private(
        unlink_intent_path(state_dir, workspace_id), {"v": 1, "at_ms": int(clock() * 1000), "linked_at": linked_at}
    )


def read_unlink_intent(state_dir: Path, workspace_id: str, host_uid: int) -> dict[str, Any]:
    """The intent record; :class:`HostError` (``custody.unsafe_mode``) when it is not a private JSON object of the
    host UID with a ``linked_at`` field (fail closed: the intent stays and keeps blocking)."""
    doc = read_json(unlink_intent_path(state_dir, workspace_id), host_uid)
    at = doc.get("linked_at", ...) if isinstance(doc, dict) else ...
    if at is ... or not (at is None or (type(at) is int and at >= 0)):
        raise unsafe(f"{unlink_intent_path(state_dir, workspace_id)}: not an unlink intent record")
    return doc


def note_alarm(
    state_dir: Path, workspace_id: str, host_uid: int, code: str, detail: str, *, clock: Any = time.time
) -> None:
    """Record an alarm in ``relay/alarms.json`` from the host side (no worker runs): the same rows as the worker's
    book (at most 64). Best effort: a write that fails is logged by the caller's own report."""
    path = relay_dir(state_dir, workspace_id) / "alarms.json"
    try:
        doc = read_json(path, host_uid)
    except HostError:
        doc = None
    rows = [r for r in (doc.get("alarms") if isinstance(doc, dict) else None) or [] if isinstance(r, dict)][:64]
    now = int(clock() * 1000)
    row = next((r for r in rows if r.get("code") == code), None)
    if row is None:
        rows = rows[:63]
        row = {"code": code, "count": 0, "first_ms": now}
        rows.append(row)
    row.update(count=int(row.get("count") or 0) + 1, last_ms=now, detail=detail[:200])
    write_private(path, {"v": 1, "alarms": rows})


def clear_unlink_intent(state_dir: Path, workspace_id: str, host_uid: int) -> None:
    from orch.store.fsio import fsync_dir

    with contextlib.suppress(FileNotFoundError):
        os.unlink(unlink_intent_path(state_dir, workspace_id))
        fsync_dir(relay_dir(state_dir, workspace_id))


def unlink_pending(state_dir: Path, workspace_id: str) -> bool:
    """An unlink intent exists (or cannot be ruled out: fail closed)."""
    try:
        os.lstat(unlink_intent_path(state_dir, workspace_id))
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


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


def start_blocker(state_dir: Path, workspace_id: str, host_uid: int) -> str | None:
    """What keeps the relay worker from starting (R5), or ``None``: ``stopped`` (the marker, or any doubt about it),
    ``not_linked`` (no ``link.json``), ``link_unsafe`` (the relay directory is not a private directory of the host
    UID with safe ancestors, or ``link.json`` is not a private JSON object of the host UID), then the R6 gate
    (:data:`orch.relay.worker.R6_GATE_REASON` while :data:`orch.relay.worker.R6_READY` is false, §11A.1). The gate is
    checked last so that ``host.status.relay`` names the operator's own stop first."""
    from orch.relay import worker

    if unlink_pending(state_dir, workspace_id):
        return "unlink_pending"
    if is_stopped(state_dir, workspace_id, host_uid):
        return "stopped"
    if not os.path.lexists(link_path(state_dir, workspace_id)):
        return "not_linked"
    try:
        ensure_private_dir(relay_dir(state_dir, workspace_id), host_uid, create=False)
        link = read_json(link_path(state_dir, workspace_id), host_uid)
    except (HostError, OSError):
        return "link_unsafe"
    if not isinstance(link, dict):
        return "link_unsafe"
    if not worker.r6_ready():
        return worker.R6_GATE_REASON
    return None


def may_start(state_dir: Path, workspace_id: str, host_uid: int) -> bool:
    """The relay worker may start (R5): :func:`start_blocker` names nothing. Anything else, including any doubt and
    the closed R6 gate, is ``False``."""
    return start_blocker(state_dir, workspace_id, host_uid) is None
