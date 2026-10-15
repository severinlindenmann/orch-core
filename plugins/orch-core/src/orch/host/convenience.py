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
from collections.abc import Callable, Iterator
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
    "refuse_pair_device",
    "pair_gate",
    "flag_path",
    "pending_handoffs",
    "read",
    "reason",
    "refuse_handoff",
    "readers_error",
    "recheck_readers",
    "refuse_reader_op",
    "set_enabled",
    "unavailable",
    "why",
    "workspace_lock",
]

REASONS = ("disabled", "relay_paired", "other_member", "phone_device", "reader_version")

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


def set_enabled(state_dir: Path, workspace_id: str, on: bool, host_uid: int) -> dict[str, Any]:
    with _flock(state_dir, "convenience.lock", host_uid):  # one flag file for every workspace: no lost update
        doc = _doc(state_dir, host_uid)
        if on:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            doc["workspaces"][workspace_id] = {"enabled": True, "at": stamp}
        else:
            doc["workspaces"].pop(workspace_id, None)
        write_private(flag_path(state_dir), doc)
        return doc


def _relay_paired(state_dir: Path, workspace_id: str) -> bool:
    """The reader reservation (orch-v2-host §11A.6): a relay link, a former one or the R6 membership state exists
    (:func:`orch.store.readerrecs.relay_level`: until H12d-3 every pairing counts as level 0)."""
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
    store: Any, state_dir: Path, workspace_id: str, host_uid: int, person: str, device: str
) -> tuple[str, list[str]] | None:
    """The first reason convenience signing is not allowed now and the readers below level 1 it names, or None.
    H12d-2 (§0.8.6 item 9.9): ``other_member`` and ``phone_device`` count only readers below level 1 (a current
    device whose reader record does not verify counts as 0); ``relay_paired`` counts a relay below level 1 (every
    pairing until H12d-3); ``reader_version`` qualifies the signing device itself."""
    from orch.ops.readers import below_level, levels

    if not enabled(state_dir, workspace_id, host_uid):
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
    "disabled": "convenience signing is off for this workspace (orch host convenience on --workspace W)",
    "relay_paired": "the workspace is paired with a relay, whose readers may not know convenience signatures",
    "other_member": "another member's reader (a core or a device) has not declared that it knows convenience "
    "signatures",
    "phone_device": "another device of yours (a phone or another machine) has not declared that it knows them",
    "reader_version": "this host's model does not accept auth convenience; update orch-core",
    "pair_device": "pairing a device (relay.pair_device) is signed in your terminal with your passphrase, never with "
    "convenience custody",
}
#: the fix of ``pair_device`` (11A.15.6 "Convenience custody is refused for pairing"): back to passphrase custody
PAIR_DEVICE_FIX = "orch keys custody passphrase --workspace W --in-place"


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


def refuse_pair_device() -> HostError:
    """``sign.convenience_unavailable`` with reason ``pair_device`` (H12c-e, 11A.15.6): ``relay.pair_device`` refuses
    ``auth:"convenience"`` whatever the flag, the readers or any dev opt-in; the fix names passphrase custody."""
    return HostError(
        "sign.convenience_unavailable",
        f"pair_device: {_TEXT['pair_device']}",
        hint=f"sign it with orch sign CODE; a key converted to convenience custody goes back with {PAIR_DEVICE_FIX}",
        detail={"reason": "pair_device", "devices": []},
    )


def check(service: Any, person: str, device: str) -> None:
    """Raise ``sign.convenience_unavailable`` (message ``<reason>: …``, detail ``{reason, devices}``) unless the
    condition holds."""
    found = why(service.store, service.state_dir, service.workspace_id, service.host_uid, person, device)
    if found is not None:
        raise unavailable(*found)


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
    if op in ("member.add", "relay.pair_device") and events is not None:
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
    if enabled(service.state_dir, service.workspace_id, service.host_uid):
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
    if enabled(service.state_dir, service.workspace_id, service.host_uid):
        return 1
    if convenience_seen(service.state_dir, service.workspace_id):
        return 1
    if history and seen_or_history(service.store, service.state_dir, service.workspace_id, service.host_uid):
        return 1
    return 0


def pair_gate(service: Any, meta: Any, *, offer_id: str, who: dict[str, str]) -> Any:
    """**The step-4 hook for R6c** (11A.15.6 step 4, H12d-3): call it when the host opens a pair request, after step 3
    (``check_pair_meta``, the meta signature, ±300 s, a presented cert) and **before** the offer is consumed, the SAS
    is shown or the primary is asked. ``who`` is ``{"pk_pin": <the link's pk_pin>}`` on path 7b, or ``{"person_id":
    <the presented cert's person, bare hex>}`` on path 7a. ``required_reader`` is computed here (the flag, the durable
    mark, the merged history: a rare op), the clock is the host's.

    Returns :class:`orch.identity.pairdevice.PairGate`: R6 stores ``decl`` and ``decl_who`` verbatim with the offer at
    step 5 (the **first** stored declaration wins on a resend: ECDSA signatures differ per resend, and a resend of a
    consumed offer never runs this again) and passes them as ``PairDeviceFacts(decl=, decl_who=)`` at step 9, where
    the authoritative check runs in the allocation's append. Raises :class:`HostError`: ``sign.convenience_readers``
    (``no_declaration``, ``reader_too_old``; ``detail`` names the device) or ``reader.decl_invalid`` (the reason
    first in the message). R6 answers either with the short ``rejected``, moves the offer to ``rejected`` and shows the
    message on the host screen ("This phone's orch app is too old for this workspace ...", §0.8.6 item 9.15 Q4)."""
    from orch.identity.pairdevice import PairGateRefused
    from orch.identity.pairdevice import pair_gate as gate

    required = _required(service, history=True)
    try:
        return gate(
            meta,
            workspace_id=service.workspace_id,
            offer_id=offer_id,
            who=who,
            required=required,
            now_ms=int(service.clock() * 1000),
        )
    except PairGateRefused as exc:
        if exc.code == "reader.decl_invalid":
            raise HostError("reader.decl_invalid", exc.detail) from None
        raise HostError(
            exc.code,
            exc.detail,
            hint="This phone's orch app is too old for this workspace (convenience signing). Update it and scan again.",
            detail={"reason": exc.reason, "devices": ["d_" + meta.device_id]},
        ) from None


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
        if "pk_pin" in ctx.get("who", {}):  # H12d-3 (9.3): a pk_pin declaration counts only under the cert's person
            _check_pin(service, ev.get("person") or entry["person"], ctx["who"]["pk_pin"])
        out[entry["device"]] = got.level
    return out


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


# -- one signing path per workspace (§0.8.6 item 8, owner decision of 12 Oct 2026)

CLICK_ONLY_TEXT = (
    "click-to-sign is on for this workspace: use Sign in the dashboard, or turn convenience off with "
    "`orch host convenience off --workspace {ws}`"
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
    """An exclusive ``flock`` on ``<state>/host/convenience-<ws>.lock``: it serializes, across processes (the host and
    ``orch host convenience on``), a hand-off prepare against switching the flag on, so exactly one wins."""
    return _flock(state_dir, f"convenience-{workspace_id}.lock", host_uid)


def refuse_handoff(service: Any) -> None:
    """``sign.click_only`` while the flag is on: a hand-off request (one with a code) is not prepared, bound or
    submitted. Call it under :func:`workspace_lock` at prepare."""
    if enabled(service.state_dir, service.workspace_id, service.host_uid):
        raise HostError(
            "sign.click_only",
            CLICK_ONLY_TEXT.format(ws=service.workspace_id),
            hint="sign in the dashboard, or: orch host convenience off --workspace W",
            fix=["orch", "host", "convenience", "off", "--workspace", service.workspace_id],
        )


_SUBMIT_GRACE = 60.0


def pending_handoffs(
    state_dir: Path, workspace_id: str, host_uid: int, *, clock: Callable[[], float] = time.time
) -> list[dict[str, Any]]:
    """The workspace's pending (``prepared`` or ``bound``, not expired) hand-off requests, as stored. A bound request
    counts a minute past its expiry as defence in depth: the host's submit holds the workspace lock from its flag
    check to its journal write, so ``on`` cannot interleave with it whatever the timing. A request file that cannot be
    read is ``custody.unsafe_mode`` (switching on must not guess about it)."""
    from .requests import PENDING, RequestStore

    store = RequestStore(state_dir, workspace_id, host_uid, clock=clock)
    out = []
    for rid in store.ids():
        try:
            rec = store.read(rid)
        except HostError as exc:
            raise unsafe(f"request {rid} cannot be read ({exc.message}); fix or remove it first") from None
        if rec is None or not rec.get("code") or rec.get("state") not in PENDING:
            continue
        try:
            if store.lapsed(rec, _SUBMIT_GRACE if rec.get("state") == "bound" else 0.0):
                continue
        except (KeyError, TypeError, ValueError):  # no usable ``expires``: unreadable, switching on must not guess
            raise unsafe(f"request {rid} has no valid expiry; fix or remove it first") from None
        out.append(rec)
    return out
