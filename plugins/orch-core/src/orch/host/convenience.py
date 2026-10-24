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
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
    "enabled",
    "flag_path",
    "read",
    "reason",
    "readers_error",
    "recheck_readers",
    "refuse_reader_op",
    "refuse_full_mode_entry",
    "refuse_turn_on",
    "registered_entry",
    "set_enabled",
    "unavailable",
    "why",
    "workspace_lock",
]

REASONS = ("disabled", "relay_paired", "other_member", "phone_device", "reader_version")
#: §11A.17.2 F3: the click signer on a dev host or against a sandbox's state (not a readers reason: never from why)
DEV_HOST = "dev_host"

AUTH = "convenience"


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


def enabled(state_dir: Path, workspace_id: str, host_uid: int) -> bool:
    try:
        entry = _doc(state_dir, host_uid)["workspaces"].get(workspace_id)
    except HostError:
        return False  # fail closed
    return isinstance(entry, dict) and entry.get("enabled") is True


def risk_ack(state_dir: Path, workspace_id: str, host_uid: int) -> str | None:
    """When the one-click risk notice was acknowledged by switching click-to-sign on (B6), else ``None``: a workspace
    enabled before B6, or one that is off."""
    try:
        entry = _doc(state_dir, host_uid)["workspaces"].get(workspace_id)
    except HostError:
        return None
    ack = entry.get("risk_ack") if isinstance(entry, dict) and entry.get("enabled") is True else None
    return ack if isinstance(ack, str) else None


def _methods(service: Any) -> Any:
    return service.store.state.workspace.settings.get("sign_methods")


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


def refuse_click_removal(service: Any, op: str, events: list[dict[str, Any]]) -> None:
    """B6 (Opus round 1, HIGH-2): a ``settings.set`` that takes ``click`` out of ``sign_methods`` is refused while
    click-to-sign is on here: the owner's key is in convenience custody and the terminal path is closed (one signing
    path per workspace), so the change would lock the honest signer out. The fix is named."""
    if op != "settings.set" or not _click_custody(service):
        return
    for ev in events:
        methods = (ev.get("set") or {}).get("sign_methods")
        if methods is not None and "click" not in methods:
            raise HostError(
                "invalid.input",
                "sign_methods cannot leave out click while click-to-sign is on for this workspace: the key is in "
                "convenience custody",
                hint="move the key back first: orch host convenience off --workspace W and "
                "orch keys custody passphrase --workspace W --in-place",
            )


def refuse_excluded_click_custody(service: Any) -> HostError | None:
    """B6 (Opus round 1, HIGH-2): while click-to-sign is on the device key is in convenience custody, and any process
    running as the person can sign with it and write any ``auth`` label. So, whatever label an event carries, nothing
    lands from this machine's person path while the workspace's ``sign_methods`` leaves ``click`` out. The custody
    decides, not the self-declared label. (With the flag off, only the key's own custody protects it: see §0.8.6.)"""
    methods = _methods(service)
    if methods is not None and "click" not in methods and _click_custody(service):
        return unavailable("disabled", [])
    return None


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
            _refuse_full_mode(store, state_dir, wid, host_uid)
            mine = _own_device(state_dir, wid)
            found = why(store, state_dir, wid, host_uid, me or "", mine, gated=True)
    finally:
        store.close()
    if found is None:
        return
    reason, devices = found
    err = unavailable(reason, devices)
    raise HostError(err.code, err.message, hint=TURN_ON_ESCAPE, detail=err.detail)


def _refuse_full_mode(store: Any, state_dir: Path, workspace_id: str, host_uid: int) -> None:
    """V3 C11 (D78/D81): click-to-sign is not switched on in a workspace in full hub mode (or ``unknown``): the click
    signer is refused at the append there, so the flag would only mislead. Called under the store lock."""
    from types import SimpleNamespace

    from . import hubops, hubsign

    view = SimpleNamespace(store=store, state_dir=state_dir, workspace_id=workspace_id, host_uid=host_uid)
    if hubops.signing_mode(view) in hubsign.HUB_ONLY:
        raise hubsign.hub_only_error()


def registered_entry(state_dir: Path, workspace_id: str, host_uid: int) -> dict[str, Any] | None:
    """The host registry entry of a workspace, or None when it is not registered on this host."""
    from .custody import read_json
    from .registry import registry_path

    for entry in (read_json(registry_path(state_dir), host_uid) or {}).get("workspaces") or []:
        if entry.get("workspace_id") == workspace_id:
            return entry
    return None


def refuse_full_mode_entry(state_dir: Path, entry: dict[str, Any], host_uid: int) -> None:
    """V3 C11: moving the key into convenience custody (``orch keys custody convenience``) is refused in a workspace in
    full hub mode (or ``unknown``): the key there must stay in passphrase custody. Fail closed on an unreadable log."""
    from orch.store import Store, StoreError

    try:
        store = Store.open(entry["root"], expected_workspace_id=entry["workspace_id"], host_state_dir=None, load="lazy")
    except (StoreError, OSError, KeyError) as exc:
        raise HostError("host.unavailable", f"the workspace log could not be read ({type(exc).__name__})") from None
    try:
        with store.locked():
            store.refresh()
            _refuse_full_mode(store, state_dir, entry["workspace_id"], host_uid)
    finally:
        store.close()


def refuse_dev_host(state_dir: Path, env: Any = None) -> None:
    """§11A.17.2 F3, D2: the click signer's switches (``orch host convenience on``, the dashboard switch) refuse on
    a dev host's or a sandbox's state, or with ``HOME`` under a sandbox root (``sign.convenience_unavailable``,
    reason ``dev_host``). The way back (``off``) never calls it."""
    from orch.devsandbox import click_refusal

    if click_refusal(state_dir, os.environ if env is None else env) is not None:
        raise unavailable(DEV_HOST, [])


def set_enabled(state_dir: Path, workspace_id: str, on: bool, host_uid: int) -> dict[str, Any]:
    if on:
        refuse_dev_host(state_dir)
    with _flock(state_dir, "convenience.lock", host_uid):  # one flag file for every workspace: no lost update
        doc = _doc(state_dir, host_uid)
        if on:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            # B6 (owner decision F): switching it on is where the risk notice is required; every path that gets here
            # shows it (the CLI prints it, the dashboard's switch shows it), and the time is recorded
            doc["workspaces"][workspace_id] = {"enabled": True, "at": stamp, "risk_ack": stamp}
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
    store: Any, state_dir: Path, workspace_id: str, host_uid: int, person: str, device: str, *, gated: bool = False
) -> tuple[str, list[str]] | None:
    """The first reason convenience signing is not allowed now and the readers below level 1 it names, or None.
    H12d-2 (§0.8.6 item 9.9): ``other_member`` and ``phone_device`` count only readers below level 1 (a current
    device whose reader record does not verify counts as 0); ``relay_paired`` counts a relay whose last verified
    level is below 1 or was never read (H12d-3); ``reader_version`` qualifies the signing device itself."""
    from orch.ops.readers import below_level, levels

    if not gated and not enabled(state_dir, workspace_id, host_uid):  # §11A.17.4: the dev gate stands in
        return "disabled", []
    methods = store.state.workspace.settings.get("sign_methods")
    if not gated and methods is not None and "click" not in methods:  # B6: the workspace does not allow one click
        return "disabled", []
    if _relay_paired(state_dir, workspace_id):
        return "relay_paired", []
    ws = store.state.workspace
    found = below_level(ws, levels(state_dir, workspace_id, store.state, host_uid), person, device, None)
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
    "and the workspace setting sign_methods must allow click)",
    "relay_paired": "the workspace is paired with a relay, whose readers may not know convenience signatures",
    "other_member": "another member's reader (a core or a device) has not declared that it knows convenience "
    "signatures",
    "phone_device": "another device of yours (a phone or another machine) has not declared that it knows them",
    "reader_version": "this host's model does not accept auth convenience; update orch-core",
    "dev_host": "this is a Simulator dev host or a dev-pair sandbox: click-to-sign is off here for good (only "
    "column R's dev gate accepts Simulator answers, orch-v2-host 11A.17)",
}


def unavailable(reason: str, devices: list[str]) -> HostError:
    """``sign.convenience_unavailable`` for ``reason`` (the message starts with it) and the readers it names."""
    named = f" ({', '.join(devices)})" if devices else ""
    hint = "sign in your terminal with orch sign CODE"
    if devices:
        hint = "update those readers and declare them (orch keys reader declare), or remove them; meanwhile " + hint
    return HostError(
        "sign.convenience_unavailable",
        f"{reason}: {_TEXT[reason]}{named}",
        hint=hint,
        detail={"reason": reason, "devices": devices},
    )


def check(service: Any, person: str, device: str, *, gate: Any = None) -> None:
    """Raise ``sign.convenience_unavailable`` (message ``<reason>: …``, detail ``{reason, devices}``) unless the
    condition holds. §11A.17: on a dev host every path but column R's gate is ``dev_host``; for a column-R event
    with a :class:`~orch.host.devmode.DevGate` of this workspace, the gate stands in for the flag (``disabled`` does
    not apply; every other reason does)."""
    from .devmode import DevGate

    gated = isinstance(gate, DevGate) and gate.workspace_id == service.workspace_id
    if getattr(service, "dev", None) is not None and not gated:
        raise unavailable(DEV_HOST, [])
    found = why(service.store, service.state_dir, service.workspace_id, service.host_uid, person, device, gated=gated)
    if found is not None:
        raise unavailable(*found)


#: V3 C2a: the ops that bind the workspace to (or free it from) a hub. The owner's own key at the terminal signs them;
#: a convenience (click-to-sign) key never does, however the label reads.
HUB_OPS = frozenset(
    {"hub.join", "hub.mode", "hub.leave", "keys.rotate", "device.rotate"}
)  # keys.rotate: C8 break-glass


def refuse_hub_op(op: str, auth: str | None) -> None:
    if op in HUB_OPS and auth == "convenience":
        raise HostError(
            "sign.convenience_unavailable",
            "joining or leaving a hub is signed with your own key at the terminal, never with click-to-sign",
            hint="first move the key back to a passphrase: orch keys custody passphrase --workspace W --in-place, "
            "then run the hub step again",
        )


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
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
