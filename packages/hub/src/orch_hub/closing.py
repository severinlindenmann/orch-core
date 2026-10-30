"""Removing the hub (#1463, design ``devB-design-1463.md`` as amended by the Opus check, protocol.md 9.7).

One persisted state machine in ``<state>/hub-removal.json`` (0600, written atomically). The steps, in this order::

    closing       writes are frozen (the guard, see :data:`WEB_ALLOWED` / :data:`LINK_ALLOWED`); nothing irreversible yet
    preflight_ok  the remover's dry run passed every check
    backed_up     the last backup, taken while the secrets, skills and tools are still in the hub (the restore point)
    withdrawn     secrets, skills and tool servers are gone (empty signed bundles at a higher version)
    notified      the signed ``hub.closing`` notice is pushed to the live workspaces; the rest pull it (``closing_get``)
    ordered       the one-shot order file is written and the detached remover started

Cancel is allowed until ``withdrawing`` (the point of no return, persisted before the withdraw runs). The web request never deletes anything: it persists ``closing`` and a background
worker does the rest; the remover (``orch host prune --hub-order``) is a separate process (see :mod:`.closing_launch`).
The order file carries no path. Nothing here puts a key, a word, a nonce or a file name into an activity row."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backup.service import BackupError, BackupService
from .config import dev_of
from .contract import closing as C
from .contract.hub import Refused
from .custodial.engine import check_channel_proof
from .keys.fsutil import StateError, read_file, write_atomic

_log = logging.getLogger("orch_hub.closing")

STATE_FILE = "hub-removal.json"
ORDER_FILE = "remove-order.json"
RESULT_FILE = "remove-result.json"
NONCE_LIFE_MS = 5 * 60_000
ORDER_LIFE_MS = 10 * 60_000
STALL_MS = 60 * 60_000  # step 6 needs a fresh confirm when the run was idle this long
STATES = ("closing", "preflight_ok", "backed_up", "withdrawing", "withdrawn", "notified", "ordered")
CANCEL_OK = frozenset({"closing", "preflight_ok", "backed_up"})

#: In ``closing`` and later the web API answers only these handlers (everything else that writes: 409 ``hub_closing``).
#: Reads (GET) are always allowed; this list is for non-GET requests.
WEB_ALLOWED = frozenset(
    {"sign_in", "sign_out", "a_hub_remove_prepare", "a_hub_remove_confirm", "a_hub_remove_cancel", "a_backup_download"}
)
#: The link ops a workspace may still use (default deny): the reads it needs to see the notice and leave.
LINK_ALLOWED = frozenset(
    {
        "snapshot_get",
        "org_skills_get",
        "mcp_servers_get",
        "org_secrets_get",
        "closing_get",
        "hub_where",
        "binding_put",
        "audit_get",
        "audit_ack",
    }
)
#: what the status page says, per step
PLAIN = {
    "closing": "Checking that the hub can be removed.",
    "preflight_ok": "Making a last backup.",
    "backed_up": "Withdrawing company secrets, skills and tools.",
    "withdrawing": "Withdrawing company secrets, skills and tools.",
    "withdrawn": "Telling the connected workspaces.",
    "notified": "Starting the removal.",
    "ordered": "Removing. This page will stop answering.",
}


class ClosingError(Exception):
    """A refused removal step; ``code`` is a wire code of the API (``hub_closing``, ``nonce_invalid``, ...)."""

    def __init__(self, code: str, plain: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.plain = plain


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Closing:
    """The removal service of one hub. All state changes happen under :attr:`_mu`; ``active()`` is cheap (memory)."""

    def __init__(
        self,
        *,
        keys: Any,
        access: Any,
        registry: Any,
        channels: Any,
        now_ms: Callable[[], int],
        launcher: Any,
        activity: Any = None,
        audit: Any = None,
        push: Callable[[str, dict[str, Any]], bool] | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.keys, self.access, self.registry, self.channels = keys, access, registry, channels
        self._now, self.launcher, self.activity, self.audit = now_ms, launcher, activity, audit
        self.push = push
        self._spawn = spawn or self._thread
        self.state_dir = Path(keys.state)
        self._mu = threading.RLock()
        self._pending: dict[str, dict[str, Any]] = {}  # user_id -> {nonce, channel_id, session, exp_ms}
        self._doc: dict[str, Any] | None = self._load()
        self._running = False

    # --- persisted state -----------------------------------------------------------------------------------------

    @staticmethod
    def _thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, name="hub-removal", daemon=True).start()

    def _path(self) -> Path:
        return self.state_dir / STATE_FILE

    def _load(self) -> dict[str, Any] | None:
        try:
            d = json.loads(read_file(self._path()))
        except StateError:
            return None
        except ValueError:
            return {"state": "closing", "removal_id": "", "fail": {"code": "state_unreadable", "plain": "The removal state could not be read."}}
        if type(d) is not dict or d.get("state") not in STATES or type(d.get("removal_id")) is not str:
            return {"state": "closing", "removal_id": "", "fail": {"code": "state_unreadable", "plain": "The removal state could not be read."}}
        return d

    def _save(self) -> None:
        if self._doc is None:
            with contextlib.suppress(FileNotFoundError):
                self._path().unlink()
            return
        write_atomic(self._path(), json.dumps(self._doc, sort_keys=True).encode())

    def active(self) -> bool:
        """True from the moment ``closing`` is persisted until a cancel removes the file (or the hub is gone)."""
        return self._doc is not None

    def phase(self) -> str | None:
        with self._mu:
            return None if self._doc is None else str(self._doc["state"])

    def _record(self, typ: str, user_id: str | None = None) -> None:
        if self.activity is None:
            return
        try:
            self.activity.record(typ, actor_kind="person" if user_id else "hub", actor_id=user_id)
        except Exception:  # noqa: BLE001, S110  # the history line is not worth failing a removal step for
            pass

    # --- preview / status ------------------------------------------------------------------------------------------

    def _backup_key_ok(self) -> bool:
        try:
            info = BackupService(self.keys, audit=self.audit, activity=self.activity, now_ms=self._now).key_info()
        except BackupError:
            return False
        return info is not None and not info["pending"]

    def preview(self) -> dict[str, Any]:
        """What the sheet shows; changes nothing and names no workspace."""
        with self._mu:
            doc = self._doc
        parts = self.launcher.parts()
        return {
            "state": "open" if doc is None else doc["state"],
            "workspaces": len(self._bound()),
            "relay": "relay" in parts,
            "publish": "publish" in parts,
            "backup_key_ok": self._backup_key_ok(),
            "backup_folder_hint": "orch-hub-backup-…",
            "cancel_allowed": doc is None or doc["state"] in CANCEL_OK,
            "refusal": None if self._backup_key_ok() else "Set up the backup words first.",
        }

    def status(self) -> dict[str, Any]:
        with self._mu:
            doc = None if self._doc is None else dict(self._doc)
        if doc is None:
            return {"state": "open", "plain": "", "told": 0, "total": 0, "cancel_allowed": False, "result": None}
        state = doc["state"]
        shown = state
        plain = PLAIN[state]
        fail = doc.get("fail")
        now = self._now()
        if fail:
            shown, plain = "failed", fail.get("plain") or "The removal stopped."
        elif state == "notified" and now > doc.get("order_ok_until_ms", 0):
            shown, plain = "stalled", "Removal is paused. Finish removing to continue."
        result = self.launcher.read_result(self.state_dir) if state == "ordered" else None
        if result is not None and not result.get("ok", False):
            shown, plain = "failed", str(result.get("message_plain") or "The removal was refused.")
        return {
            "state": shown,
            "plain": plain,
            "told": len(doc.get("told", [])),
            "total": int(doc.get("total", 0)),
            "cancel_allowed": state in CANCEL_OK,
            "result": result,
        }

    # --- prepare / confirm ------------------------------------------------------------------------------------------

    def prepare(self, user_id: str, channel_id: str, session_token: str) -> dict[str, Any]:
        """Mint the single-use nonce (one pending per user: a new prepare replaces the old) and return what the page signs."""
        with self._mu:
            if self._doc is not None and not self._can_reconfirm():
                raise ClosingError("hub_closing")
            if not self._backup_key_ok():
                raise ClosingError("no_backup_key", "Set up the backup words first.")
            now = self._now()
            nonce = secrets.token_hex(16)
            exp = now + NONCE_LIFE_MS
            self._pending[user_id] = {"nonce": nonce, "channel_id": channel_id, "session": _sha(session_token), "exp_ms": exp}
            ctx = C.remove_context(self.keys.hub_id, nonce, exp, user_id)
            return {"ctx": ctx, "digest": C.remove_digest(ctx).hex(), "nonce": nonce, "exp_ms": exp}

    def _can_reconfirm(self) -> bool:
        """A run that failed or stalled before the order may be confirmed again ("Finish removing")."""
        d = self._doc
        if d is None:
            return False
        return d["state"] != "ordered" and (bool(d.get("fail")) or self._now() > d.get("order_ok_until_ms", 0))

    def check_nonce(self, user_id: str, nonce: object, session_token: str) -> None:
        """Cheap checks before a code is spent: the nonce is this user's, this session's, unexpired and unused."""
        with self._mu:
            p = self._pending.get(user_id)
            if (
                p is None
                or type(nonce) is not str
                or not hmac.compare_digest(p["nonce"], nonce)
                or p["session"] != _sha(session_token)
                or self._now() >= p["exp_ms"]
            ):
                raise ClosingError("nonce_invalid")

    def confirm(self, user_id: str, nonce: str, proof: str, session_token: str) -> dict[str, Any]:
        """The channel key's proof over the removal digest, then the persisted ``closing``. The nonce is consumed here, in
        the same step that creates the state file, under one lock (two admins cannot both start)."""
        with self._mu:
            self.check_nonce(user_id, nonce, session_token)
            p = self._pending[user_id]
            ctx = C.remove_context(self.keys.hub_id, p["nonce"], p["exp_ms"], user_id)
            try:
                check_channel_proof(
                    keys=self.keys,
                    channels=self.channels,
                    user_id=user_id,
                    channel_id=p["channel_id"],
                    digest=C.remove_digest(ctx),
                    proof=proof,
                    now_ms=self._now(),
                )
            except Refused:
                del self._pending[user_id]  # a wrong proof burns the nonce: no oracle on one digest
                raise
            del self._pending[user_id]  # consumed: single use
            now = self._now()
            if self._doc is None:
                if not self._backup_key_ok():
                    raise ClosingError("no_backup_key", "Set up the backup words first.")
                self._doc = {
                    "v": 1,
                    "state": "closing",
                    "removal_id": secrets.token_hex(16),
                    "user_id": user_id,
                    "started_ms": now,
                    "step_ms": now,
                    "order_ok_until_ms": now + STALL_MS,
                    "told": [],
                    "total": 0,
                }
                self._save()
                self._record("hub.removal.started", user_id)
            elif self._can_reconfirm():
                self._doc.pop("fail", None)
                self._doc["order_ok_until_ms"] = now + STALL_MS
                self._doc["step_ms"] = now
                self._save()
            else:
                raise ClosingError("hub_closing")
            self._start()
            return {"ok": True, "removal_id_set": True}

    def cancel(self, user_id: str) -> None:
        with self._mu:
            if self._doc is None:
                return
            if self._doc["state"] not in CANCEL_OK:
                raise ClosingError(
                    "removal_started",
                    "Too late to cancel: removing the hub is in progress.",
                )
            self._doc = None
            self._save()
            self._record("hub.removal.cancelled", user_id)

    # --- the worker -------------------------------------------------------------------------------------------------

    def resume(self) -> None:
        """Hub start: a persisted run that is not at ``ordered`` goes on from where it was."""
        with self._mu:
            if self._doc is not None and not self._doc.get("fail") and self._doc["state"] != "ordered":
                self._start()

    def _start(self) -> None:
        with self._mu:
            if self._running:
                return
            self._running = True
        self._spawn(self._run)

    def _advance(self, state: str) -> None:
        with self._mu:
            if self._doc is None:
                raise ClosingError("cancelled")
            self._doc["state"] = state
            self._doc["step_ms"] = self._now()
            self._save()

    def _fail(self, code: str, plain: str) -> None:
        with self._mu:
            if self._doc is not None:
                self._doc["fail"] = {"code": code, "plain": plain}
                self._save()
        self._record("hub.removal.failed")

    def run_sync(self) -> None:
        """Run the remaining steps on this thread (tests)."""
        with self._mu:
            self._running = True
        self._run()

    def _run(self) -> None:
        try:
            self._steps()
        except ClosingError as e:
            if e.code != "cancelled":
                self._fail(e.code, e.plain or "The removal stopped.")
        except Exception:  # noqa: BLE001
            _log.exception("hub removal step failed")
            self._fail("removal_failed", "The removal stopped. The hub is unchanged or safe to retry.")
        finally:
            with self._mu:
                self._running = False

    def _state(self) -> str:
        with self._mu:
            if self._doc is None:
                raise ClosingError("cancelled")
            return str(self._doc["state"])

    def _steps(self) -> None:
        if self._state() == "closing":  # 2 preflight: the remover's dry run
            self._preflight()
            self._advance("preflight_ok")
        if self._state() == "preflight_ok":  # 3 the last backup, still WITH the secrets (the restore point)
            self._backup()
            self._advance("backed_up")
        if self._state() == "backed_up":  # 4 withdraw: the point of no return is persisted FIRST (cancel refuses from here)
            self._advance("withdrawing")
        if self._state() == "withdrawing":  # also the resume point after a crash: finish forward, never back to open
            self._withdraw()
            self._advance("withdrawn")
        if self._state() == "withdrawn":  # 5 notify
            self._notify()
            self._advance("notified")
        if self._state() == "notified":  # 6 order
            with self._mu:
                ok = self._now() <= self._doc.get("order_ok_until_ms", 0)  # type: ignore[union-attr]
            if not ok:
                return  # stalled: needs a new prepare/confirm
            self._order()
            self._advance("ordered")

    def _preflight(self) -> None:
        with self._mu:
            doc = dict(self._doc or {})
        self.launcher.write_order(self.state_dir, self._order_doc(doc))
        try:
            ok, plain = self.launcher.dry_run(self.state_dir)
        finally:
            self.launcher.remove_order(self.state_dir)
        if not ok:
            raise ClosingError("preflight_failed", plain)

    def _backup(self) -> None:
        user = None
        with self._mu:
            user = (self._doc or {}).get("user_id")
        try:
            BackupService(self.keys, audit=self.audit, activity=self.activity, now_ms=self._now).make(user, 0)
        except BackupError:
            raise ClosingError("backup_failed", "The last backup could not be made. Nothing was removed.") from None

    def _withdraw(self) -> None:
        from .access.orgmcp import org_mcp
        from .access.orgsecrets import org_secrets
        from .access.orgskills import org_skills

        a = self.access
        org_secrets(a).withdraw_all(self._now())
        org_skills(a).withdraw_all()
        org_mcp(a).withdraw_all()

    def _bound(self) -> list[str]:
        out = []
        for ws in sorted(self.registry.pins()):
            st = self.registry.binding_state(ws)
            if st is not None and st.get("status") == "bound":
                out.append(ws)
        return out

    def _notify(self) -> None:
        told: list[str] = []
        bound = self._bound()
        for ws in bound:
            try:
                note = self.notice_for(ws)
                if note is not None and self.push is not None and self.push(ws, note):
                    told.append(ws)
            except Exception:  # noqa: BLE001  # never blocks: the rest pull it (closing_get)
                _log.warning("closing notice not pushed to a workspace")
        with self._mu:
            if self._doc is not None:
                self._doc["told"], self._doc["total"] = told, len(bound)
                self._save()

    def _order_doc(self, doc: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        return {
            "v": 1,
            "kind": "orch.hub_remove_order.v1",
            "hub_id": self.keys.hub_id,
            "removal_id": doc.get("removal_id") or secrets.token_hex(16),
            "state_dir": os.path.realpath(self.state_dir),
            "issued_ms": now,
            "exp_ms": now + ORDER_LIFE_MS,
            "dev": bool(dev_of(self.keys)),
        }

    def _order(self) -> None:
        with self._mu:
            doc = dict(self._doc or {})
        self.launcher.write_order(self.state_dir, self._order_doc(doc))
        try:
            self.launcher.launch(self.state_dir, doc["removal_id"])
        except Exception:
            self.launcher.remove_order(self.state_dir)  # cannot start the remover: the hub stays up in closing
            raise ClosingError("launch_failed", "The removal could not be started. The hub is still running.") from None
        self._record("hub.removal.ordered")

    # --- the notice (push and pull) ---------------------------------------------------------------------------------

    def notice_for(self, workspace_id: str) -> dict[str, Any] | None:
        """A fresh signed ``hub.closing`` for ONE bound workspace while the hub is in ``notified`` or ``ordered`` (else None).
        Signed per call (7 days from now) with the workspace's CURRENT ``binding_seq``; ``removal_id`` is the run's."""
        with self._mu:
            doc = None if self._doc is None else dict(self._doc)
        if doc is None or doc["state"] not in ("withdrawn", "notified", "ordered"):
            return None  # before the withdrawal nothing is told (a cancel must stay possible)
        st = self.registry.binding_state(workspace_id)
        if st is None or st.get("status") != "bound":
            return None
        o = C.closing_object(
            hub_id=self.keys.hub_id,
            op_cert_id=self.keys.op_cert()["o"]["cert_id"],
            workspace_id=workspace_id,
            binding_seq=int(st["binding_seq"]),
            removal_id=doc["removal_id"],
            issued_ms=self._now(),
            dev=bool(dev_of(self.keys)),
        )
        return self.keys.op_signer.sign_object(o)  # type: ignore[no-any-return]


__all__ = ["CANCEL_OK", "Closing", "ClosingError", "LINK_ALLOWED", "ORDER_FILE", "RESULT_FILE", "STATE_FILE", "WEB_ALLOWED"]
