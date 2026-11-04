"""The host's side of click-to-sign (orch-v2-host §0.8.6 item 1, D-P2-23): the per-workspace flag and the readers
condition. The host never touches a person's key: this module only decides whether ``auth: "convenience"`` may enter a
request.

* The flag: ``<state>/host/convenience.json`` (0600, host UID), ``{"v":1, "workspaces":{"<ws>":{"enabled":true,
  "at":"…"}}}``, written by ``orch host convenience on|off --workspace W``. Off until switched on.
* The condition, checked at ``human.prepare`` with a convenience signer, at a convenience binding in ``human.get`` and
  at ``human.submit`` of convenience events: every reader of the log is local and updated. The first failing reason
  wins: ``disabled``, ``relay_paired``, ``other_member``, ``phone_device``, ``reader_version``
  (``sign.convenience_unavailable``).
* While the flag is on, ops that add a reader are refused at prepare (``sign.convenience_readers``): switch it off
  first.
  With convenience events in the history they are refused in every mode (:mod:`orch.ops.readers`).
* Idle switch-off (#1198 C17): the flag also goes off by itself after ``idle_hours`` (default 12, 1..168, a host-local
  setting beside the flag, not a signed workspace setting) without a successful One-click signature. "Off" means
  exactly what the dashboard's "Turn off" without a passphrase means: the flag no longer counts (``enabled`` is false,
  ``why`` says ``idle``), the key stays where it is (no key loss, no custody move), and the person turns One click on
  again to continue. It is computed from the stored ``last_used`` at every read (sign time, status), so there is
  nothing to run in the background and no write on a read. ``last_used`` is host wall-clock seconds, because the
  host, the CLI and the signer are separate processes and a monotonic clock does not compare between them; a
  ``last_used`` more than :data:`SKEW` seconds in the future counts as idle (fails closed).
  This limits a forgotten session; it is not a boundary against other processes of the same user until agents run
  as their own user (H1): such a process can edit the file or run ``orch host convenience on``.
* Turning it on again (the dashboard's plain click, :meth:`orch.click.signer.ClickSigner.custody_on` with ``resume``)
  goes through :func:`refuse_turn_on` and :func:`set_enabled`, which restarts the idle clock and the risk notice.
"""

from __future__ import annotations

import contextlib
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orch import oslock
from orch.ops.readers import (  # one list for the host and the op handlers
    READER_OPS,
    convenience_seen,
    seen_or_history,
)

from .custody import ensure_private_dir, read_json, unsafe, write_private
from .errors import HostError
from .registry import host_dir

__all__ = [
    "AUTH",
    "READER_OPS",
    "REASONS",
    "check",
    "DEFAULT_IDLE_HOURS",
    "enabled",
    "flag_path",
    "idle_hours",
    "idle_off",
    "set_idle_hours",
    "touch",
    "read",
    "reason",
    "readers_error",
    "recheck_readers",
    "refuse_reader_op",
    "refuse_turn_on",
    "registered_entry",
    "set_enabled",
    "unavailable",
    "why",
    "workspace_lock",
]

REASONS = ("idle", "disabled", "relay_paired", "other_member", "phone_device", "reader_version")
#: §11A.17.2 F3: the click signer on a dev host or against a sandbox's state (not a readers reason: never from why)

AUTH = "convenience"

#: #1198 C17: hours without a successful One-click signature after which the flag goes off (the design default)
DEFAULT_IDLE_HOURS = 12
IDLE_HOURS_RANGE = (1, 168)
#: a ``last_used`` this far ahead of the clock is not believed (a clock set back, a hand edit): idle
SKEW = 300
#: ``touch`` writes at most this often: the smallest idle limit is an hour
TOUCH_EVERY = 60


def _now() -> float:
    return time.time()


def flag_path(state_dir: Path) -> Path:
    return host_dir(state_dir) / "convenience.json"


def _doc(state_dir: Path, host_uid: int) -> dict[str, Any]:
    doc = read_json(flag_path(state_dir), host_uid)
    if doc is None:
        return {"v": 1, "workspaces": {}}
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("workspaces"), dict):
        raise HostError("internal", "the convenience flag file is corrupt; refusing convenience signing")
    return doc


def read(state_dir: Path, host_uid: int) -> dict[str, Any]:
    """The flag file as stored (``{"v":1,"workspaces":{…}}``); refuses a corrupt one."""
    return _doc(state_dir, host_uid)


def _entry(state_dir: Path, workspace_id: str, host_uid: int) -> dict[str, Any] | None:
    entry = _doc(state_dir, host_uid)["workspaces"].get(workspace_id)
    return entry if isinstance(entry, dict) and entry.get("enabled") is True else None


def idle_hours(state_dir: Path, workspace_id: str, host_uid: int) -> int:
    """The idle limit in hours (:data:`DEFAULT_IDLE_HOURS` unless set); an invalid stored value is the default."""
    try:
        value = ((_doc(state_dir, host_uid).get("settings") or {}).get(workspace_id) or {}).get("idle_hours")
    except (HostError, AttributeError):
        return DEFAULT_IDLE_HOURS
    ok = type(value) is int and IDLE_HOURS_RANGE[0] <= value <= IDLE_HOURS_RANGE[1]
    return value if ok else DEFAULT_IDLE_HOURS


def _idle_expired(entry: dict[str, Any], hours: int, now: float) -> bool:
    last = entry.get("last_used")
    if type(last) is not int:  # switched on before C17, or hand-edited: counts from nothing, so it is idle
        return True
    if last > now + SKEW:
        return True
    return now - last > hours * 3600


def idle_off(state_dir: Path, workspace_id: str, host_uid: int, *, now: float | None = None) -> int | None:
    """The hours the flag was idle for when it switched itself off, or ``None``: it is on and in use, or off for
    another reason. Fails closed to ``None`` only for an unreadable file (``enabled`` is false there anyway)."""
    try:
        entry = _entry(state_dir, workspace_id, host_uid)
    except HostError:
        return None
    if entry is None:
        return None
    hours = idle_hours(state_dir, workspace_id, host_uid)
    return hours if _idle_expired(entry, hours, _now() if now is None else now) else None


def enabled(state_dir: Path, workspace_id: str, host_uid: int) -> bool:
    """Is One click on here: the flag is set and it has not gone idle (:func:`idle_off`)."""
    try:
        entry = _entry(state_dir, workspace_id, host_uid)
    except HostError:
        return False  # fail closed
    return entry is not None and not _idle_expired(entry, idle_hours(state_dir, workspace_id, host_uid), _now())


def touch(state_dir: Path, workspace_id: str, host_uid: int) -> None:
    """Record a successful One-click signature now (resets the idle clock). Only while the flag is on and not idle: an
    expired flag is never revived by activity. Never raises: a failed write only means the flag may go idle sooner."""
    try:
        now = int(_now())
        entry = _entry(state_dir, workspace_id, host_uid)
        if entry is None or not enabled(state_dir, workspace_id, host_uid):
            return
        last = entry.get("last_used")
        if type(last) is int and 0 <= now - last < TOUCH_EVERY:
            return
        with _flock(state_dir, "convenience.lock", host_uid):
            doc = _doc(state_dir, host_uid)
            current = doc["workspaces"].get(workspace_id)
            if (
                isinstance(current, dict)
                and current.get("enabled") is True
                and not _idle_expired(current, idle_hours(state_dir, workspace_id, host_uid), float(now))
            ):  # again under the lock: right at the limit activity must not revive the flag
                current["last_used"] = now
                write_private(flag_path(state_dir), doc)
    except (HostError, OSError):
        return


def set_idle_hours(state_dir: Path, workspace_id: str, hours: int, host_uid: int) -> dict[str, Any]:
    """Set the idle limit (``invalid.input`` outside 1..168). It takes effect at the next read, also for a flag that is
    on now (a longer limit does not revive one that has already gone idle)."""
    lo, hi = IDLE_HOURS_RANGE
    if type(hours) is not int or not lo <= hours <= hi:
        raise HostError("invalid.input", f"idle hours must be a whole number from {lo} to {hi}")
    with _flock(state_dir, "convenience.lock", host_uid):
        doc = _doc(state_dir, host_uid)
        settings = doc.setdefault("settings", {})
        if not isinstance(settings, dict):
            raise HostError("internal", "the convenience flag file is corrupt; refusing convenience signing")
        settings[workspace_id] = {"idle_hours": hours}
        write_private(flag_path(state_dir), doc)
        return doc


def risk_ack(state_dir: Path, workspace_id: str, host_uid: int) -> str | None:
    """When the one-click risk notice was acknowledged by switching click-to-sign on (B6), else ``None``: a workspace
    enabled before B6, or one that is off."""
    try:
        entry = _doc(state_dir, host_uid)["workspaces"].get(workspace_id)
    except HostError:
        return None
    ack = entry.get("risk_ack") if isinstance(entry, dict) and entry.get("enabled") is True else None
    return ack if isinstance(ack, str) else None


def _click_custody(service: Any) -> bool:
    """B6 (Opus round 2): one-click signing is possible here when the flag is on OR the person's key still sits in
    convenience custody (the dashboard's "Turn off" leaves it there). Fails closed: an unreadable selection counts."""
    if enabled(service.state_dir, service.workspace_id, service.host_uid):
        return True
    from orch.custody import select

    try:
        sel = select.read_selection(service.state_dir, service.workspace_id)
    except Exception:  # noqa: BLE001 - fail closed on an unreadable or invalid selection
        return True
    # a custody transition in progress (either direction) counts too: the convenience copy may already exist
    return sel is not None and (sel.get("custody") == "convenience" or sel.get("transition") is not None)


def flag_or_gate(service: Any) -> bool:
    """``required_reader`` 1 (§0.8.6 item 9.4): the per-workspace flag is on, or the Simulator dev gate passes for
    this workspace (§11A.17.4, the lock-free :func:`orch.host.devmode.gated`)."""
    from .devmode import gated

    return enabled(service.state_dir, service.workspace_id, service.host_uid) or gated(service)


#: the way out of a refused switch-on (B10, the lockout fix): update the readers, or stay with the passphrase key
TURN_ON_ESCAPE = (
    "update the orch app on those devices (it then declares what it reads) or remove them, then switch click-to-sign "
    "on again; to stay without it: orch host convenience off --workspace W and, if the key was moved, "
    "orch keys custody passphrase --workspace W --in-place"
)


def _own_device(state_dir: Path, workspace_id: str) -> str:
    """This machine's device for the workspace (the public part of its key, no passphrase asked); ``""`` when it cannot
    be found, so that nothing is excluded and the answer can only be stricter."""
    try:
        from orch.identity import device_ref
        from orch.ops.human import KEY_ID, device_backend

        return device_ref(device_backend(state_dir, workspace_id, convenience=True).public_key(KEY_ID))
    except Exception:  # noqa: BLE001
        return ""


def refuse_turn_on(state_dir: Path, entry: dict[str, Any], host_uid: int, *, person: str | None = None) -> None:
    """B10 (the lockout fix): switching click-to-sign on is refused **up front** while any reader of the workspace is
    below level 1 (an old phone app, ``no_declaration``, a relay too old), naming the devices, instead of leaving the
    flag on with every click signature refused. The same readers rule as every convenience signature
    (:func:`why`, with the flag treated as on), read from the verified log and the reader records: the machine's own
    device is the signer and does not count. Fail closed: a log or record that cannot be read refuses. Raises
    ``sign.convenience_unavailable`` (reason ``relay_paired`` / ``other_member`` / ``phone_device`` and ``devices``)
    whose hint names the escape (:data:`TURN_ON_ESCAPE`). The same check runs again at every signature."""
    from orch.store import Store, StoreError

    wid = entry["workspace_id"]
    me = person
    if me is None:
        me = ((entry.get("peers") or {}).get(str(os.getuid())) or {}).get("person")
    try:
        store = Store.open(entry["root"], expected_workspace_id=wid, host_state_dir=None, load="lazy")
    except StoreError as exc:
        raise HostError(exc.code, exc.detail or exc.code) from None
    except (OSError, KeyError) as exc:
        raise HostError("host.unavailable", f"the workspace log could not be read ({type(exc).__name__})") from None
    try:
        with store.locked():
            store.refresh()
            mine = _own_device(state_dir, wid)
            found = why(store, state_dir, wid, host_uid, me or "", mine, gated=True)
    finally:
        store.close()
    if found is None:
        return
    reason, devices = found
    err = unavailable(reason, devices)
    raise HostError(err.code, err.message, hint=TURN_ON_ESCAPE, detail=err.detail)


def registered_entry(state_dir: Path, workspace_id: str, host_uid: int) -> dict[str, Any] | None:
    """The host registry entry of a workspace, or None when it is not registered on this host."""
    from .custody import read_json
    from .registry import registry_path

    for entry in (read_json(registry_path(state_dir), host_uid) or {}).get("workspaces") or []:
        if entry.get("workspace_id") == workspace_id:
            return entry
    return None


def set_enabled(state_dir: Path, workspace_id: str, on: bool, host_uid: int) -> dict[str, Any]:
    with _flock(state_dir, "convenience.lock", host_uid):  # one flag file for every workspace: no lost update
        doc = _doc(state_dir, host_uid)
        if on:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # B6 (owner decision F): switching it on is where the risk notice is required; every path that gets here
            # shows it (the CLI prints it, the dashboard's switch shows it), and the time is recorded
            doc["workspaces"][workspace_id] = {
                "enabled": True,
                "at": stamp,
                "risk_ack": stamp,
                "last_used": int(_now()),  # C17: switching on starts the idle clock
            }
        else:
            doc["workspaces"].pop(workspace_id, None)
        write_private(flag_path(state_dir), doc)
        return doc


def _relay_paired(state_dir: Path, workspace_id: str) -> bool:
    """The reader reservation (orch-v2-host §11A.6): a relay link, a former one or the R6 membership state exists
    (:func:`orch.store.readerrecs.relay_level`): its last verified level is below 1, or was never checked (H12d-3)."""
    from orch.store.readerrecs import relay_level

    level = relay_level(state_dir, workspace_id)
    return level is not None and level < 1


def _model_knows() -> bool:
    try:
        from orch.custody.base import AUTH_VALUES
        from orch.schema import load

        enum = load("common")["$defs"]["auth"]["enum"]
    except Exception:  # noqa: BLE001 - any doubt is "the model does not know it"
        return False
    return AUTH in enum and AUTH in AUTH_VALUES


def why(
    store: Any,
    state_dir: Path,
    workspace_id: str,
    host_uid: int,
    person: str,
    device: str,
    *,
    gated: bool = False,
    removing: frozenset[str] = frozenset(),
    ignore_relay: bool = False,
) -> tuple[str, list[str]] | None:
    """The first reason convenience signing is not allowed now and the readers below level 1 it names, or None.
    H12d-2 (§0.8.6 item 9.9): ``other_member`` and ``phone_device`` count only readers below level 1 (a current
    device whose reader record does not verify counts as 0); ``relay_paired`` counts a relay whose last verified
    level is below 1 or was never read (H12d-3); ``reader_version`` qualifies the signing device itself."""
    from orch.ops.readers import below_level, levels

    if not gated and not enabled(state_dir, workspace_id, host_uid):  # §11A.17.4: the dev gate stands in
        return ("idle", []) if idle_off(state_dir, workspace_id, host_uid) is not None else ("disabled", [])
    # ignore_relay: leaving the hub (hub.left) by click; a full-mode workspace is relay paired by definition, and only
    # that reason is waived (other_member, phone_device, reader_version stay)
    if not ignore_relay and _relay_paired(state_dir, workspace_id):
        return "relay_paired", []
    ws = store.state.workspace
    found = below_level(ws, levels(state_dir, workspace_id, store.state, host_uid), person, device, None, removing)
    if found is not None:
        return found
    if not _model_knows():
        return "reader_version", []
    return None


def reason(store: Any, state_dir: Path, workspace_id: str, host_uid: int, person: str, device: str) -> str | None:
    """The first reason convenience signing is not allowed now, or None (:func:`why`)."""
    found = why(store, state_dir, workspace_id, host_uid, person, device)
    return None if found is None else found[0]


_TEXT = {
    "disabled": "convenience signing is off for this workspace (orch host convenience on --workspace W; "
    "turn on One click)",
    "relay_paired": "the workspace is paired with a relay, whose readers may not know convenience signatures",
    "other_member": "another member's reader (a core or a device) has not declared that it knows convenience "
    "signatures",
    "phone_device": "another device of yours (a phone or another machine) has not declared that it knows them",
    "reader_version": "this host's model does not accept auth convenience; update orch-core",
    "idle": "One click switched itself off after {hours} hours without use. Turn it on again to continue",
}


def unavailable(reason: str, devices: list[str], *, hours: int | None = None) -> HostError:
    """``sign.convenience_unavailable`` for ``reason`` (the message starts with it) and the readers it names. ``idle``
    names the limit in ``hours`` (C17)."""
    named = f" ({', '.join(devices)})" if devices else ""
    hint = "sign in your terminal with the action’s CLI command"
    if reason == "idle":
        return HostError(
            "sign.convenience_unavailable",
            f"idle: {_TEXT['idle'].format(hours=hours)}.",
            hint="orch host convenience on --workspace W (or use the action’s CLI command meanwhile)",
            detail={"reason": "idle", "devices": []},  # the hours are in the message
        )
    if devices:
        hint = "update those readers and declare them (orch keys reader declare), or remove them; meanwhile " + hint
    return HostError(
        "sign.convenience_unavailable",
        f"{reason}: {_TEXT[reason]}{named}",
        hint=hint,
        detail={"reason": reason, "devices": devices},
    )


def check(
    service: Any,
    person: str,
    device: str,
    *,
    gate: Any = None,
    removing: frozenset[str] = frozenset(),
    leaving: bool = False,
) -> None:
    """Raise ``sign.convenience_unavailable`` (message ``<reason>: …``, detail ``{reason, devices}``) unless the
    condition holds. §11A.17: for a column-R event
    with a :class:`~orch.host.devmode.DevGate` of this workspace, the gate stands in for the flag (``disabled`` does
    not apply; every other reason does)."""
    from .devmode import DevGate

    gated = isinstance(gate, DevGate) and gate.workspace_id == service.workspace_id
    found = why(
        service.store, service.state_dir, service.workspace_id, service.host_uid, person, device, gated=gated,
        removing=removing, ignore_relay=leaving,
    )  # fmt: skip
    if found is not None:
        if found[0] == "idle":
            raise unavailable("idle", [], hours=idle_hours(service.state_dir, service.workspace_id, service.host_uid))
        raise unavailable(*found)


#: the break-glass ops the owner's own key signs at the terminal; a convenience (click-to-sign) key never does,
#: however the label reads. (The hub ops are not here: since #1198 C9 they carry the owner proof, whatever the key.)
TERMINAL_OPS = frozenset({"keys.rotate", "device.rotate"})  # keys.rotate: C8 break-glass


def refuse_terminal_op(op: str, auth: str | None) -> None:
    if op in TERMINAL_OPS and auth == "convenience":
        raise HostError(
            "sign.convenience_unavailable",
            "this step is signed with your own key at the terminal, never with click-to-sign",
            hint="first move the key back to a passphrase: orch keys custody passphrase --workspace W --in-place, "
            "then run the step again",
        )


def refuse_hub_reader(service: Any, op: str, card: Any, *, now_ms: int, dev_mode: bool) -> None:
    """#1198 C9 (hub contract 5 "Reader level"): ``hub.join`` and ``hub.mode`` bind the workspace to a hub whose card
    declares a ``reader`` level below the workspace's ``required_reader`` (convenience history or the flag on): that
    hub could not read the log, so it is ``sign.convenience_readers`` reason ``hub_too_old``. A card that does not
    verify is left to the op's own refusal (``hub.refused``)."""
    if op not in ("hub.join", "hub.mode"):
        return
    from orch.hubclient import wire
    from orch.hubclient.errors import HubRefused

    try:
        held = wire.verify_card(card, now_ms=now_ms, dev_mode=dev_mode)
    except HubRefused:
        return
    required = _required(service, history=True)
    try:
        wire.check_hub_reader(held, required)
    except HubRefused:
        raise HostError(
            "sign.convenience_readers",
            f"hub_too_old: this hub declares reader level {held['card']['reader']}, this workspace requires {required}",
            hint="the hub must be updated to a release that knows convenience signatures (card reader 1), or join "
            "before this workspace has convenience signatures, with convenience off",
            detail={"reason": "hub_too_old", "devices": []},
        ) from None


def refuse_reader_op(
    service: Any,
    op: str,
    *,
    history: bool = False,
    events: list[dict[str, Any]] | None = None,
    readers: list[dict[str, Any]] | None = None,
    relay_level: int | None = None,
) -> None:
    """The early reader checks of an op that adds a reader (§0.8.6 item 1, H12d-2 and H12d-3 item 9.4).

    ``member.add`` with its prepared ``events`` (H12d-2): the new reader's declaration (``readers``, verified again
    here without the clock, 9.3) against ``required_reader``: 1 while the flag is on, the durable mark is set or, with
    ``history`` (at binding and at submit, under the store lock), the merged history holds a convenience event. A
    reader of level 1 is admitted with convenience history and while the flag is on; a missing or lower declaration
    is ``sign.convenience_readers`` (``no_declaration``, ``reader_too_old``). The append checks it again
    (:func:`orch.host.human.append_signed`), authoritatively.

    ``relay.pair_device`` (H12c-e) with its events: the same as ``member.add``, with the phone's pair declaration.

    ``relay.link`` (H12d-3, the relaxed rule of 9.4): the relay's level from ``GET /v2/relay-info`` (``relay_level``,
    read at link step 1; ``None``, not read, counts as 0) against ``required_reader``; below it is
    ``sign.convenience_readers`` reason ``relay_too_old``. A relay of level 1 is linked with convenience history and
    while the flag is on.

    ``relay.pair`` (H12d-3): no early refusal at the offer (it carries no declaration yet). The phone's pair
    declaration is gated when the host opens the pair request (:func:`pair_gate`, 11A.15.6 step 4) and,
    authoritatively, in the R6 allocation's append (``relay.pair_device``).

    ``member.add`` without its events (the early call before its handler ran): refused while the mark is set, the
    history (``history``) holds a convenience event or the flag is on; the handler's own check follows."""
    if op not in READER_OPS:
        return
    if op == "member.add" and events is not None:
        _check_new_readers(service, events, readers or [], history=history)
        return
    if op == "relay.pair":
        return
    if op == "relay.link":
        required = _required(service, history=history)
        level = 0 if relay_level is None else relay_level
        if required >= 1 and level < required:
            raise HostError(
                "sign.convenience_readers",
                f"relay_too_old: the relay declares reader level {level} (GET /v2/relay-info), this workspace "
                f"requires {required}",
                hint="the relay must be updated to a release that knows convenience signatures (relay-info reader 1)"
                ", or link this workspace before it has convenience signatures with convenience off",
                detail={"reason": "relay_too_old", "devices": []},
            )
        return
    seen = convenience_seen(service.state_dir, service.workspace_id)  # #441: the durable mark, in every check
    reason = "no_declaration"
    if seen or (history and seen_or_history(service.store, service.state_dir, service.workspace_id, service.host_uid)):
        raise HostError(
            "sign.convenience_readers",
            f"{reason}: {op} would add a reader to a workspace whose history has convenience signatures",
            hint="the new reader must understand auth convenience first (orch-v2-host §0.8.6)." + READER_UPGRADE,
            detail={"reason": reason, "devices": []},
        )
    if flag_or_gate(service):  # §11A.17.4: the dev gate counts as the flag
        raise HostError(
            "sign.convenience_readers",
            f"{reason}: {op} adds a reader while convenience signing is on for this workspace",
            hint="switch it off first: orch host convenience off --workspace W",
            fix=["orch", "host", "convenience", "off", "--workspace", service.workspace_id],
            detail={"reason": reason, "devices": []},
        )


def _required(service: Any, *, history: bool) -> int:
    """``required_reader`` (9.4) for an early check: the flag or the durable mark, and with ``history`` a scan of the
    merged history (which sets the mark when it finds one)."""
    if flag_or_gate(service):  # §11A.17.4: 1 while the dev gate passes, as while the flag is on
        return 1
    if convenience_seen(service.state_dir, service.workspace_id):
        return 1
    if history and seen_or_history(service.store, service.state_dir, service.workspace_id, service.host_uid):
        return 1
    return 0


def recheck_readers(service: Any, events: list[dict[str, Any]], readers: list[dict[str, Any]]) -> dict[str, int]:
    """Verify each admitted declaration again against the event that adds its device (no clock: a binding, a submit,
    9.3), and return device -> level. A declaration that no longer verifies is ``reader.decl_invalid``."""
    from orch.identity.reader import DeclInvalid
    from orch.ops.readers import READER_EVENTS, admission_context, verify_admission

    out: dict[str, int] = {}
    for entry in readers:
        ev = next(
            (
                e
                for e in events
                if e.get("type") in READER_EVENTS
                and "d_" + str(((e.get(READER_EVENTS[e["type"]]) or {}).get("o") or {}).get("device_id"))
                == entry.get("device")
            ),
            None,
        )
        if ev is None:
            raise HostError("internal", "a reader declaration names no device the request adds")
        try:
            ctx = admission_context(entry)  # the entry's source: enrol/log, or pair/view with its who and bind
            got = verify_admission(
                entry["decl"],
                workspace_id=service.workspace_id,
                cert=ev[READER_EVENTS[ev["type"]]],
                person=ev.get("person") or entry["person"],
                **ctx,
                now_ms=None,
            )
        except (DeclInvalid, KeyError, TypeError, ValueError) as exc:
            if not isinstance(exc, DeclInvalid):
                raise HostError("internal", "an admitted reader entry is malformed") from None
            raise HostError("reader.decl_invalid", exc.detail) from None
        if got.digest != entry.get("digest") or got.level != entry.get("level"):
            raise HostError("internal", "the stored reader declaration changed")
        if entry.get("source") == "pair":  # H12d-3 (9.3, #555 review F3): who names the cert's person or its key
            _check_pair_who(service, ev[READER_EVENTS[ev["type"]]], ctx["who"])
        out[entry["device"]] = got.level
    return out


def _check_pair_who(service: Any, cert: Any, who: Any) -> None:
    """A pair declaration counts only for the certificate's person, as a stored record does (``record_level``):
    ``who`` is ``{"person_id": <the cert's person>}`` (path 7a) or ``{"pk_pin": <the pin of that person's key>}``
    (path 7b); anything else is ``reader.decl_invalid`` reason ``who`` (#555 review F3)."""
    o = cert.get("o") if isinstance(cert, dict) else None
    pid = o.get("person_id") if isinstance(o, dict) else None
    if not isinstance(pid, str) or not isinstance(who, dict):
        raise HostError("reader.decl_invalid", "who: the certificate names no person")
    if who == {"person_id": pid}:
        return
    if set(who) == {"pk_pin"} and isinstance(who["pk_pin"], str):
        _check_pin(service, "p_" + pid, who["pk_pin"])
        return
    raise HostError("reader.decl_invalid", "who: the pair declaration names another person than the certificate's")


def _check_pin(service: Any, person: Any, pin: str) -> None:
    """A pairing declaration signed for the link's ``pk_pin`` (path 7b, before the phone knew the person key) is
    admitted only when the certificate's person is the member whose person key that pin names (9.3: "the host checks
    in the R6 allocation that the certificate it got is under the key that pin names"); else ``reader.decl_invalid``
    reason ``who``."""
    from orch import crypto

    try:
        pk_pub = service.store.state.person_key(person) if isinstance(person, str) else None
        have = None if pk_pub is None else crypto.b64u(crypto.pk_pin(crypto.unb64u(pk_pub, crypto.PUB_LEN)))
    except (crypto.CryptoError, ValueError, TypeError, AttributeError):
        have = None
    if have is None or have != pin:  # public values: a plain comparison
        raise HostError("reader.decl_invalid", "who: the pairing's pk_pin does not name the certificate's person key")


def readers_error(exc: Any) -> HostError:
    """A :class:`orch.ops.readers.ReadersRefused` as the host's error (with the closed ``detail``)."""
    return HostError(exc.code, exc.message, hint=exc.hint, detail=exc.detail)


def _check_new_readers(service: Any, events: list[dict[str, Any]], readers: list[dict[str, Any]], *, history: bool):
    from orch.ops.readers import ReadersRefused, check_readers

    required = _required(service, history=history)
    lv: dict[str, int | None] = dict(recheck_readers(service, events, readers))
    try:
        check_readers(service.store.state.workspace, lv, [_no_auth(e) for e in events], required=required,
                      relay_level=None)  # fmt: skip
    except ReadersRefused as exc:
        raise readers_error(exc) from None


def _no_auth(e: dict[str, Any]) -> dict[str, Any]:
    """The reader part only: a convenience signer is checked by :func:`check` (prepare, binding, submit)."""
    return {k: v for k, v in e.items() if k != "auth"}


READER_UPGRADE = (
    " A member's core declares with orch keys reader declare --enrol (member.add --reader-decl); a relay declares its"
    " level in GET /v2/relay-info and a phone in its pair request (H12d-3)."
)


LOCK_WAIT = 2.0  # seconds a lock is waited for before ``lock.busy``: a stopped process must not wedge the host


@contextlib.contextmanager
def _flock(state_dir: Path, name: str, host_uid: int) -> Iterator[None]:
    """An exclusive ``flock`` on ``<state>/host/<name>``, waited for at most :data:`LOCK_WAIT` seconds (``lock.busy``,
    exit 9, retryable). The file must be a regular 0600 file of the host UID, opened without following a link
    (``custody.unsafe_mode``)."""
    ensure_private_dir(host_dir(state_dir), host_uid)
    path = host_dir(state_dir) / name
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        raise unsafe(f"{path}: {exc.strerror or exc}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != host_uid or stat.S_IMODE(info.st_mode) & 0o077:
            raise unsafe(f"{path}: expected a 0600 regular file of UID {host_uid}")
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                oslock.flock(fd, oslock.LOCK_EX | oslock.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise HostError("lock.busy", f"{path} is held by another process", hint="try again") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def workspace_lock(state_dir: Path, workspace_id: str, host_uid: int) -> contextlib.AbstractContextManager[None]:
    """An exclusive ``flock`` on ``<state>/host/convenience-<ws>.lock``: it serializes, across processes (the host,
    ``orch host convenience on`` and the click signer's custody switch), changes of the flag and of the custody
    selection, so exactly one wins."""
    return _flock(state_dir, f"convenience-{workspace_id}.lock", host_uid)
