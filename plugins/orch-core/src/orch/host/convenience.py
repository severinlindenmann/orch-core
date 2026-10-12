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
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from orch.ops.readers import READER_OPS, has_convenience_events  # one list for the host and the op handlers

from .custody import ensure_private_dir, read_json, write_private
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
    "pending_handoffs",
    "refuse_handoff",
    "refuse_reader_op",
    "workspace_lock",
    "set_enabled",
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
    ensure_private_dir(host_dir(state_dir), host_uid)
    doc = _doc(state_dir, host_uid)
    if on:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        doc["workspaces"][workspace_id] = {"enabled": True, "at": stamp}
    else:
        doc["workspaces"].pop(workspace_id, None)
    write_private(flag_path(state_dir), doc)
    return doc


def _relay_paired(state_dir: Path, workspace_id: str) -> bool:
    """P3 keeps the relay membership state in ``hosts/<ws>/membership/`` (§11); its presence means a pairing."""
    return (Path(state_dir) / "hosts" / workspace_id / "membership").exists()


def _model_knows() -> bool:
    try:
        from orch.custody.base import AUTH_VALUES
        from orch.schema import load

        enum = load("common")["$defs"]["auth"]["enum"]
    except Exception:  # noqa: BLE001 - any doubt is "the model does not know it"
        return False
    return AUTH in enum and AUTH in AUTH_VALUES


def reason(store: Any, state_dir: Path, workspace_id: str, host_uid: int, person: str, device: str) -> str | None:
    """The first reason convenience signing is not allowed now, or None."""
    if not enabled(state_dir, workspace_id, host_uid):
        return "disabled"
    if _relay_paired(state_dir, workspace_id):
        return "relay_paired"
    ws = store.state.workspace
    if any(p != person for p in ws.members):
        return "other_member"
    for ref, dev in ws.devices.items():
        if ref != device and not dev.removed and not dev.revoked:
            return "other_member" if dev.person != person else "phone_device"
    if not _model_knows():
        return "reader_version"
    return None


_TEXT = {
    "disabled": "convenience signing is off for this workspace (orch host convenience on --workspace W)",
    "relay_paired": "the workspace is paired with a relay, whose readers may not know convenience signatures",
    "other_member": "another member (or their device) reads this workspace and may not know convenience signatures",
    "phone_device": "you have another device in this workspace (a phone or another machine) that may not know them",
    "reader_version": "this host's model does not accept auth convenience; update orch-core",
}


def check(service: Any, person: str, device: str) -> None:
    """Raise ``sign.convenience_unavailable`` (message ``<reason>: …``) unless the condition holds."""
    why = reason(service.store, service.state_dir, service.workspace_id, service.host_uid, person, device)
    if why is not None:
        raise HostError(
            "sign.convenience_unavailable",
            f"{why}: {_TEXT[why]}",
            hint="sign in your terminal with orch sign CODE",
        )


def refuse_reader_op(service: Any, op: str, *, history: bool = False) -> None:
    """While the flag is on, an op that adds a reader is refused: switch convenience off first (owner decision). With
    ``history`` (at binding and at submit, under the store lock) a history holding convenience events refuses too."""
    if op not in READER_OPS:
        return
    if history and has_convenience_events(service.store):
        raise HostError(
            "sign.convenience_readers",
            f"{op} would add a reader to a workspace whose history has convenience signatures",
            hint="the new reader must understand auth convenience first (orch-v2-host §0.8.6)",
        )
    if enabled(service.state_dir, service.workspace_id, service.host_uid):
        raise HostError(
            "sign.convenience_readers",
            f"{op} adds a reader while convenience signing is on for this workspace",
            hint="switch it off first: orch host convenience off --workspace W",
            fix=["orch", "host", "convenience", "off", "--workspace", service.workspace_id],
        )


# -- one signing path per workspace (§0.8.6 item 8, owner decision of 12 Oct 2026)

CLICK_ONLY_TEXT = (
    "click-to-sign is on for this workspace: use Sign in the dashboard, or turn convenience off with "
    "`orch host convenience off --workspace {ws}`"
)


@contextlib.contextmanager
def workspace_lock(state_dir: Path, workspace_id: str, host_uid: int) -> Iterator[None]:
    """An exclusive ``flock`` on ``<state>/host/convenience-<ws>.lock``: it serializes, across processes (the host and
    ``orch host convenience on``), a hand-off prepare against switching the flag on, so exactly one wins."""
    ensure_private_dir(host_dir(state_dir), host_uid)
    fd = os.open(
        host_dir(state_dir) / f"convenience-{workspace_id}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


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


_now = time.time  # the CLI's clock for request expiry (tests set it to the lab's clock)


def pending_handoffs(state_dir: Path, workspace_id: str, host_uid: int) -> list[dict[str, Any]]:
    """The workspace's pending (``prepared`` or ``bound``, not expired) hand-off requests, as stored."""
    from .requests import PENDING, RequestStore

    store = RequestStore(state_dir, workspace_id, host_uid, clock=_now)
    now, out = _now(), []
    for rid in store.ids():
        try:
            rec = store.read(rid)
        except HostError:
            continue
        if rec is None or not rec.get("code") or rec.get("state") not in PENDING:
            continue
        if not rec.get("submit") and now >= float(rec.get("expires", 0)):
            continue
        out.append(rec)
    return out
