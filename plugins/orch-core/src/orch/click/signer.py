"""The click signer's state machine (orch-v2-host §0.8.6 item 5): ``prepare``, ``view``, ``sign``, ``cancel``,
``status``. One instance per process; the channel (:mod:`orch.click.server`) calls one method at a time.

What it guarantees (and what not, §0.8.6 item 0): it signs only requests it prepared itself on its own host connection,
after every §0.8.1 check, and only the stored immutable signing inputs whose View the caller names by ``view_digest``.
It cannot know whether a person saw the View: the dashboard is trusted with that.

Durability (§0.8.6 item 5, "Durable sign records"): before the crypto signing a claim (``view_digest``, op,
idem, expiry) is synced to disk, and before a signature leaves the signer the record of the signed events replaces it
(:mod:`orch.click.records`). Every ``sign`` consults it first, so a request is signed at most once per
``(request_id, view_digest)`` even across a crash and a restart; a claim that never completed is never signed again.
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch import canon

from . import view as views
from .protocol import (
    CONTRACT,
    DEPOSIT_MAX_HOURS,
    DEPOSIT_REASONS,
    FEATURES,
    HASH,
    IDEM,
    OP_NAME,
    REASONS,
    REQ,
    WS_ID,
    ClickError,
    op_class,
    refused,
)
from .records import RecordError, Records, is_claim

__all__ = ["Approval", "Budget", "ClickSigner"]

KEY_ID = "dk"
MAX_APPROVALS = 16
DWELL_SECONDS = 1.0
PER_MINUTE = 30
PER_HOUR = 600
#: the custody switch (§0.8.6 item 3a): wrong passphrases at ``custody_on`` per window, and a passphrase's length
CUSTODY_TRIES = 5
CUSTODY_WINDOW = 900.0
PASSPHRASE_MAX = 1024
REQUEST_LIFETIME = 600.0  # §0.8.1: a request lives 10 minutes
HUMAN_OPS_V1 = ("human.prepare", "human.get", "human.submit", "human.pending", "human.cancel")


#: the readers-condition reasons that mean "a reader is below level 1" (§0.8.6 item 9.9): ``status`` shows
#: ``readers_old`` for the workspace until a call succeeds again (H12d-2, 9.11)
READERS_OLD = frozenset({"relay_paired", "other_member", "phone_device"})


class Budget:
    """Event signatures per signer instance: ``PER_MINUTE`` and ``PER_HOUR`` on monotonic time, reserved for a whole
    request before signing. Not a per-person limit (several dashboards have several budgets)."""

    def __init__(self, mono: Callable[[], float], per_minute: int = PER_MINUTE, per_hour: int = PER_HOUR) -> None:
        self.mono, self.limits = mono, ((60.0, per_minute), (3600.0, per_hour))
        self.spent: deque[tuple[float, int]] = deque()

    def reserve(self, n: int) -> None:
        now = self.mono()
        while self.spent and self.spent[0][0] <= now - 3600.0:
            self.spent.popleft()
        for window, limit in self.limits:
            inside = [(t, k) for t, k in self.spent if t > now - window]
            used = sum(k for _, k in inside)
            if used + n > limit:
                wait = (inside[0][0] + window - now) if inside else window
                raise ClickError(
                    "rate.limited",
                    f"at most {limit} signatures per {int(window)} s from this signer",
                    retry_after_ms=max(1, int(wait * 1000)),
                    retryable=True,
                )
        self.spent.append((now, n))


@dataclass
class Approval:
    workspace_id: str
    request_id: str
    op: str
    kind: str
    binding: bytes  # views.invariant(prepared) at prepare
    prepared: dict[str, Any]  # the canonical prepared request, as validated at prepare
    gen: int
    deadline: float  # monotonic
    expires_at: str
    files: dict[str, Any] | None
    view_digest: str | None = None
    built_at: float | None = None
    signed: list[dict[str, Any]] | None = None  # exact signed events, kept until a terminal answer
    idem: str | None = None
    result: dict[str, Any] | None = None
    log: list[str] = field(default_factory=list)
    #: v1.6: prepared in deposit-only grant mode (D-P2-27); the hand-over's settled outcome once known
    deposit: bool = False
    deposit_result: dict[str, Any] | None = None


def _new_idem() -> str:
    from orch.host.control_client import new_idem

    return new_idem()


class ClickSigner:
    """``connect(entry) -> connection`` (``.call(op, args, idem=, files=)`` and ``.welcome``), ``custody(state, ws)``
    -> ``(backend, selection)``; both are seams for tests. ``workspaces()`` lists the person's own registry entries."""

    def __init__(
        self,
        state_dir: Path,
        *,
        uid: int | None = None,
        connect: Callable[[dict[str, Any]], Any] | None = None,
        custody: Callable[[Path, str], tuple[Any, Any]] | None = None,
        workspaces: Callable[[], list[dict[str, Any]]] | None = None,
        enabled: Callable[[dict[str, Any]], bool] | None = None,
        execution: Callable[[dict[str, Any]], bool] | None = None,
        mono: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        log: Any = None,
        records: Records | None = None,
    ) -> None:
        self.state = Path(state_dir)
        self.uid = os.getuid() if uid is None else uid
        self._connect = connect or self._default_connect
        self._custody = custody or self._default_custody
        self._workspaces = workspaces or self._default_workspaces
        self._enabled = enabled or self._default_enabled
        #: v1.6: this machine has dashboard execution on for the workspace (the deposit grant's precondition)
        self._execution = execution or self._default_execution
        self.mono, self.wall = mono, wall
        self.budget = Budget(mono)
        self.approvals: dict[tuple[str, str], Approval] = {}
        self.conns: dict[str, Any] = {}
        self.busy = False
        #: ``(workspace_id, request_id)`` whose signing started in this process: defence in depth against a claim
        #: record deleted from outside after a failed or interrupted signature (the claim file alone is not trusted)
        #: Kept for the whole signer process lifetime, never pruned: memory is bounded by the sign budget (30/min,
        #: 600/h) and an entry is one tiny key, while a pruned marker would reopen the second-signature window.
        self._attempted: set[tuple[str, str]] = set()
        self.log = log
        self.records = records if records is not None else Records(self.state, wall=wall)
        self.records.prune()  # retention runs at start and at prepare, never on the sign path
        #: the last connection or custody problem per workspace, for ``status`` (it loads no key and connects nowhere)
        self.problems: dict[str, str] = {}
        #: monotonic times of wrong passphrases at ``custody_on`` (:meth:`_custody_budget`)
        self.custody_misses: deque[float] = deque()

    # -- seams ---------------------------------------------------------------------------------------------------------
    def _default_workspaces(self) -> list[dict[str, Any]]:
        from orch.host.handoff import _own_workspaces

        return _own_workspaces(self.state, self.uid, None)

    def _default_connect(self, entry: dict[str, Any]) -> Any:
        from orch.host.client import Connection, Pin
        from orch.host.handoff import _socket

        wid = entry["workspace_id"]
        path = _socket(None, self.state, int(entry["host_uid"]), wid)
        return Connection(Pin(int(entry["host_uid"]), wid, path), client="orch click")

    @staticmethod
    def _default_custody(state: Path, workspace_id: str) -> tuple[Any, Any]:
        from orch.custody.select import device_backend

        return device_backend(state, workspace_id)

    def _default_enabled(self, entry: dict[str, Any]) -> bool:
        from orch.host.convenience import enabled

        return enabled(self.state, entry["workspace_id"], int(entry["host_uid"]))

    def _default_execution(self, entry: dict[str, Any]) -> bool:
        from orch.runner import enablement

        return bool(enablement.state_of(self.state, entry["workspace_id"], int(entry["host_uid"]))["enabled"])

    # -- helpers -------------------------------------------------------------------------------------------------------
    def _note(self, what: str, **kw: Any) -> None:
        if self.log is not None:
            self.log.write(what, **kw)

    def _entry(self, workspace_id: Any) -> dict[str, Any]:
        if type(workspace_id) is not str or not WS_ID.fullmatch(workspace_id):
            raise ClickError("invalid.input", "workspace_id is a workspace id")
        for e in self._workspaces():
            if e.get("workspace_id") == workspace_id:
                return e
        raise ClickError("not_found", "this workspace is not in your own registry")

    def _person(self, entry: dict[str, Any]) -> str:
        return str(entry["peers"][str(self.uid)]["person"])

    def _names(self, entry: dict[str, Any]) -> Callable[[str], tuple[str, str] | None]:
        wid = entry["workspace_id"]
        name = Path(str(entry.get("root") or wid)).name
        prefix = str(entry.get("prefix"))
        return lambda w: (name, prefix) if w == wid else None

    def _conn(self, entry: dict[str, Any]) -> Any:
        wid = entry["workspace_id"]
        conn = self.conns.get(wid)
        if conn is None:
            try:
                conn = self._connect(entry)
            except ClickError:
                raise
            except Exception as exc:  # noqa: BLE001 - not served, refused, a pin failure
                raise ClickError("host.unavailable", f"cannot reach the workspace: {type(exc).__name__}") from None
            try:
                self._check_welcome(conn.welcome)
            except ClickError:
                conn.close()
                self.problems[wid] = "version_mismatch"
                raise
            self.conns[wid] = conn
            if self.problems.get(wid) == "version_mismatch":
                self.problems.pop(wid, None)
        return conn

    @staticmethod
    def _check_welcome(welcome: Mapping[str, Any]) -> None:
        from orch.host.dispatch import served_ops
        from orch.host.service import _digest

        ops = welcome.get("ops") or {}
        if welcome.get("proto") != 1 or any(ops.get(o) != 1 for o in HUMAN_OPS_V1):
            raise ClickError("host.version_mismatch", "the host does not serve the human ops this signer speaks")
        mine = _digest(served_ops())
        if welcome.get("registry") != mine:
            raise ClickError(
                "host.version_mismatch",
                f"the signer's orch-core differs from the host's (registry {str(welcome.get('registry'))[:19]}…)",
            )

    def _call(self, entry: dict[str, Any], op: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        from orch.host.errors import HostError

        conn = self._conn(entry)
        wid = entry["workspace_id"]
        try:
            res = conn.call(op, args, **kw)
            if self.problems.get(wid) == "readers_old":
                self.problems.pop(wid, None)
            return res
        except HostError as exc:
            reason = None
            if exc.code == "sign.convenience_unavailable":
                head = exc.message.split(":", 1)[0]
                reason = head if head in REASONS else None
                if reason in READERS_OLD:  # H12d-2 (§0.8.6 item 9.11): status names it until a call succeeds
                    self.problems[wid] = "readers_old"
            # #517: the host's detail only for the two convenience codes, re-validated whole by ClickError
            raise ClickError(exc.code, exc.message, reason=reason, retryable=exc.retryable, detail=exc.detail) from None
        except (OSError, ConnectionError) as exc:
            self.conns.pop(entry["workspace_id"], None)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            raise ClickError("host.unavailable", f"the workspace connection broke: {type(exc).__name__}") from None

    def _convenience(self, entry: dict[str, Any]) -> tuple[Any, Any]:
        from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound

        try:
            backend, sel = self._custody(self.state, entry["workspace_id"])
        except KeyNotFound as exc:
            raise ClickError("custody.no_key", str(exc)) from None
        except BackendUnavailable as exc:
            self.problems[entry["workspace_id"]] = "custody_unavailable"
            raise ClickError("custody.unavailable", str(exc)) from None
        except CustodyError as exc:
            self.problems[entry["workspace_id"]] = "custody_unavailable"
            raise ClickError("custody.unavailable", str(exc)) from None
        if getattr(backend, "auth", None) != "convenience" or sel is None:
            raise ClickError(
                "custody.no_key",
                "this workspace's device key is not in convenience custody (orch keys custody convenience)",
            )
        return backend, sel

    def _approval(self, args: Mapping[str, Any]) -> tuple[dict[str, Any], Approval]:
        entry = self._entry(args.get("workspace_id"))
        rid = args.get("request_id")
        if type(rid) is not str or not REQ.fullmatch(rid):
            raise ClickError("invalid.input", "request_id is a request id")
        approval = self.approvals.get((entry["workspace_id"], rid))
        if approval is None:
            raise refused("not_prepared_here", "this signer did not prepare that request; use orch sign CODE")
        return entry, approval

    def _expired(self, approval: Approval) -> bool:
        return self.mono() >= approval.deadline

    def _fetch(self, entry: dict[str, Any], approval: Approval, device: str) -> dict[str, Any]:
        """``human.get`` with this signer (already bound at prepare) and the byte-for-byte binding check."""
        prepared = self._call(
            entry, "human.get", {"request_id": approval.request_id, "signer": {"device": device, "auth": "convenience"}}
        )["data"]
        if views.invariant(prepared) != approval.binding:
            raise refused("view_changed", "the host's answer differs from the request this signer prepared")
        views.check_prepared(
            prepared,
            workspace_id=approval.workspace_id,
            person=self._person(entry),
            device=device,
            files=approval.files,
            deposit=approval.deposit,
        )
        return prepared

    # -- the ops -------------------------------------------------------------------------------------------------------
    def prepare(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._entry(args.get("workspace_id"))
        op, inner, idem, files = args.get("op"), args.get("args"), args.get("idem"), args.get("files")
        if type(op) is not str or not OP_NAME.fullmatch(op) or not isinstance(inner, dict):
            raise ClickError("invalid.input", "prepare takes op and args")
        if type(idem) is not str or not IDEM.fullmatch(idem):
            raise ClickError("invalid.input", "prepare takes the dashboard's idem (a_<ULID>)")
        if files is not None and (
            not isinstance(files, dict) or any(not isinstance(v, dict) or set(v) != {"b64"} for v in files.values())
        ):
            raise ClickError("invalid.input", "files are {name: {b64}}")
        deposit = args.get("deposit", False)
        if type(deposit) is not bool:
            raise ClickError("invalid.input", "deposit is true or false")
        if deposit:  # v1.6 (D-P2-27): the one click grant, handed straight to the runner, never shown
            if op != "grant":
                raise ClickError("invalid.input", "deposit is for grant only")
            inner = self._deposit_args(entry, inner)
        elif op_class(op) == "terminal":
            raise refused("terminal_only", f"{op} is signed in the terminal (orch sign CODE)")
        if len(self.approvals) >= MAX_APPROVALS:
            self._sweep()
            if len(self.approvals) >= MAX_APPROVALS:
                raise ClickError("rate.limited", "too many open sign requests", retry_after_ms=10_000, retryable=True)
        self.records.prune()
        _, sel = self._convenience(entry)
        device = sel["device"]
        body = {"op": op, "args": inner, "signer": {"device": device, "auth": "convenience"}}
        prepared = self._call(entry, "human.prepare", body, idem=idem, files=files or None)
        prepared = canon.loads_strict(canon.dumps(prepared["data"]))
        request = prepared.get("request") or {}
        rid = request.get("id")
        if type(rid) is not str or not REQ.fullmatch(rid) or request.get("code") is not None:
            raise refused("check_failed", "the host's answer is not a request bound to this signer")
        if prepared.get("op") != op:
            raise refused("check_failed", "the host prepared another op than the one asked for")
        if request.get("state") != "bound":
            raise ClickError("human.request_state", f"the request is {request.get('state')}")
        kind = views.check_prepared(
            prepared,
            workspace_id=entry["workspace_id"],
            person=self._person(entry),
            device=device,
            files=files,
            deposit=deposit,
        )
        key = (entry["workspace_id"], rid)
        if key not in self.approvals:  # an idem retry returns the same request: keep its record
            expires_at = str(request.get("expires_at"))
            left = min(max(0.0, _epoch(expires_at) - self.wall()), REQUEST_LIFETIME)  # never longer than §0.8.1's
            self.approvals[key] = Approval(
                workspace_id=entry["workspace_id"],
                request_id=rid,
                op=op,
                kind=kind,
                binding=views.invariant(prepared),
                prepared=prepared,
                gen=sel.gen,
                deadline=self.mono() + left,
                expires_at=expires_at,
                files=files or None,
                deposit=deposit,
            )
        approval = self.approvals[key]
        if approval.deposit != deposit:
            raise ClickError("idem.conflict", "this idem named a request prepared in another mode")
        self._note("prepared", workspace=entry["workspace_id"], request=rid, op=op)
        return {"request_id": rid, "expires_at": approval.expires_at, "op": op, "confirm": approval.kind}

    def view(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, approval = self._approval(args)
        if self._expired(approval):
            self.approvals.pop((approval.workspace_id, approval.request_id), None)
            raise refused("expired", "the request expired; prepare it again")
        _, sel = self._convenience(entry)
        if sel.gen != approval.gen:
            self._drop(approval)
            raise refused("custody_changed", "the device key's custody changed; prepare the request again")
        prepared = self._fetch(entry, approval, sel["device"])
        v = self._build(entry, approval, prepared, sel["device"])
        approval.view_digest, approval.built_at = views.digest(v), self.mono()
        self._note("review", workspace=approval.workspace_id, request=approval.request_id, op=approval.op)
        return {"view": v, "view_digest": approval.view_digest}

    def _build(
        self, entry: dict[str, Any], approval: Approval, prepared: dict[str, Any], device: str
    ) -> dict[str, Any]:
        artifacts = None
        if any(e.get("type") in ("artifact.added", "artifact.replaced") for e in prepared["events"]):
            artifacts = "checked" if approval.files else "unseen"
        return views.build(
            prepared,
            self._names(entry),
            kind=approval.kind,
            device=device,
            expires_at=approval.expires_at,
            artifacts=artifacts,
            deposit=approval.deposit,
        )

    def sign(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.busy:
            raise refused("busy", "another signature is in progress")
        self.busy = True
        try:
            return self._sign(args)
        finally:
            self.busy = False

    def _record(self, workspace_id: str, request_id: str) -> dict[str, Any] | None:
        try:
            return self.records.get(workspace_id, request_id)
        except RecordError as exc:
            raise refused("record_unavailable", f"{exc}; this signer does not sign that request again") from None

    def _sign(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry = self._entry(args.get("workspace_id"))
        rid = args.get("request_id")
        if type(rid) is not str or not REQ.fullmatch(rid):
            raise ClickError("invalid.input", "request_id is a request id")
        vd = args.get("view_digest")
        if type(vd) is not str or not HASH.fullmatch(vd):
            raise ClickError("invalid.input", "view_digest is a sha256 hash")
        rec = self._record(entry["workspace_id"], rid)  # first, before the registry: it survives a restart
        if rec is not None:
            return self._deposited(entry, rec["op"], rid, self._replay(entry, rec, vd))
        entry, approval = self._approval(args)
        if approval.signed is not None:  # the in-memory guard stays, also when the record was lost
            if vd != approval.view_digest:
                raise refused("already_signed", "this request was already signed for another review")
            if approval.result is not None:
                return self._deposited(entry, approval.op, rid, {"result": approval.result, "duplicate": True})
            self._rerecord(approval)
            # the identical events and idem, never a new signature
            return self._deposited(entry, approval.op, rid, self._submit(entry, approval, None))
        if (approval.workspace_id, approval.request_id) in self._attempted:
            # signing started here, and neither a record nor cached events exist: the claim was removed from outside
            raise refused(
                "record_unavailable",
                "signing this request was attempted but no record of it exists; this signer does not sign it again",
            )
        if self._expired(approval):
            self.approvals.pop((approval.workspace_id, approval.request_id), None)
            raise refused("expired", "the request expired; prepare it again")
        if approval.view_digest is None or vd != approval.view_digest:
            raise refused("view_changed", "that is not the review this signer built for the request")
        if approval.built_at is None or self.mono() - approval.built_at < DWELL_SECONDS:
            raise refused("too_soon", "the review was on screen for less than a second")
        backend, sel = self._convenience(entry)
        if sel.gen != approval.gen:
            self._drop(approval)
            raise refused("custody_changed", "the device key's custody changed; prepare the request again")
        prepared = self._fetch(entry, approval, sel["device"])
        if views.digest(self._build(entry, approval, prepared, sel["device"])) != approval.view_digest:
            raise refused("view_changed", "the review changed since it was shown (a check or the registry); view again")
        if approval.kind == "word":
            expected = views.word(prepared, self._names(entry))
            if args.get("word") != expected:
                raise refused("word", "type the word the review shows")
        stored = approval.prepared  # the immutable inputs validated at prepare (== prepared, checked above)
        self.budget.reserve(len(stored["events"]))
        from orch.host import signer as host_signer

        idem = _new_idem()
        try:  # durable before the crypto signing: a failure or crash after this never signs the request again
            claim = self.records.claim(
                approval.workspace_id,
                approval.request_id,
                view_digest=approval.view_digest,
                op=approval.op,
                idem=idem,
                expires_at=approval.expires_at,
            )
        except RecordError as exc:  # nothing was signed
            raise refused("record_unavailable", f"{exc}; nothing was signed") from None
        if claim["idem"] != idem:  # another signer process claimed or recorded it first: never a second signature
            self._note("adopted", workspace=approval.workspace_id, request=approval.request_id)
            return self._deposited(entry, approval.op, rid, self._replay(entry, claim, approval.view_digest))
        # From here the claim is never released: an exception does not prove that no signature exists (the host signer
        # signs event by event), so any failure leaves the claim and that request fails closed.
        # before any signature can exist
        self._attempted.add((approval.workspace_id, approval.request_id))
        signed = host_signer.sign(stored, backend=backend, key_id=KEY_ID, workspace_id=approval.workspace_id)
        # the secret (a deposit grant only) stays in this frame: never in the approval, a record, a log or a result
        secret = signed.secret
        if secret is not None and not approval.deposit:  # a grant is refused earlier; never near the channel
            raise refused("terminal_only", "a request that draws a secret is signed in the terminal")
        approval.signed, approval.idem = signed.events, idem  # in memory at once: a retry resends, never re-signs
        try:
            try:  # durable before the signature leaves: a crash after this resends these events, never signs again
                rec = self.records.complete(claim, signed.events)
            except RecordError as exc:  # nothing left the signer; a sign in this process retries the completion
                raise refused("record_unavailable", f"{exc}; nothing was submitted") from None
            self._note("signed", workspace=approval.workspace_id, request=approval.request_id)
            res = self._replay(entry, rec, approval.view_digest)  # the commit first, then the hand-over
            return self._deposited(entry, approval.op, rid, res, secret)
        finally:
            del secret, signed  # best effort: Python cannot wipe a str; nothing keeps a reference to it

    def _deposit_args(self, entry: dict[str, Any], inner: dict[str, Any]) -> dict[str, Any]:
        """A deposit grant's arguments: ``hours`` 1..12 (default 8) and ``scope``; the signer sets ``label:"runner"``
        and ``verbs:"agent"`` itself. Refused (``terminal_only``) unless this machine has dashboard execution on."""
        unknown = set(inner) - {"hours", "scope", "label", "verbs", "runner"}
        if inner.get("runner", True) is not True:
            raise ClickError("invalid.input", "a deposit grant is a runner grant")
        hours, scope = inner.get("hours", 8), inner.get("scope", "workable")
        if unknown or inner.get("label", "runner") != "runner" or inner.get("verbs", "agent") != "agent":
            raise ClickError("invalid.input", "a deposit grant takes hours and scope only")
        if type(hours) is not int or scope not in ("all", "workable"):
            raise ClickError("invalid.input", "hours is a whole number, scope is all or workable")
        if not 1 <= hours <= DEPOSIT_MAX_HOURS:
            raise refused("terminal_only", f"a click grant lasts at most {DEPOSIT_MAX_HOURS} hours; use the terminal")
        if not self._execution(entry):
            raise refused("terminal_only", "dashboard execution is off for this workspace on this machine")
        # runner:true makes the op itself a runner grant (label runner, at most 12 h, verbs agent, no ORCH_GRANT hint)
        return {"hours": hours, "scope": scope, "verbs": "agent", "label": "runner", "runner": True}

    def _deposited(
        self, entry: dict[str, Any], op: str, rid: str, res: dict[str, Any], secret: str | None = None
    ) -> dict[str, Any]:
        """A deposit grant's answer gains ``deposit``, ``deposit_id`` (the request's ULID) and ``grant``. With the
        secret (this very signature): ``grant.deposit`` on the signer's own connection, after the commit. Without
        it (a repeated ``sign``, a restart): the settled outcome, else ``grant.deposit_status`` once."""
        if op != "grant":
            return res
        wid = entry["workspace_id"]
        approval = self.approvals.get((wid, rid))
        if approval is not None and approval.deposit_result is not None:
            return {**res, **approval.deposit_result}
        did = rid[3:]
        data = (res.get("result") or {}).get("data") or {}
        gid = data.get("grant") if isinstance(data.get("grant"), str) else None
        outcome, reason = "unknown", None
        if secret is not None:
            gid = gid or secret.partition(".")[0]
            outcome, reason = self._hand_over(entry, did, secret)
        if outcome == "unknown":
            outcome, reason = self._deposit_status(entry, did)
        self._note("deposit", workspace=wid, request=rid, code=outcome)
        out: dict[str, Any] = {"deposit": outcome, "deposit_id": did, "grant": gid}
        if outcome == "rejected":  # a static code of a closed set, never host text (contract 2)
            out["deposit_reason"] = reason if reason in DEPOSIT_REASONS else "other"
        if approval is not None and outcome in ("confirmed", "rejected"):
            approval.deposit_result = out
        return {**res, **out}

    def _hand_over(self, entry: dict[str, Any], deposit_id: str, secret: str) -> tuple[str, str | None]:
        """``(outcome, reason)``: ``reason`` is the host's refusal code (mapped to the closed set by the caller)."""
        try:
            got = self._call(entry, "grant.deposit", {"deposit_id": deposit_id}, auth={"grant": secret})["data"]
        except ClickError as exc:
            if exc.code in ("host.unavailable", "outcome.unknown", "timeout"):
                return "unknown", None
            self._note("deposit", workspace=entry["workspace_id"], code=exc.code)
            return "rejected", exc.code
        except Exception:  # noqa: BLE001 - after the commit nothing may fail falsely: the outcome is unknown
            self._drop_conn(entry)
            return "unknown", None
        outcome = got.get("deposit")
        if outcome not in ("confirmed", "rejected", "unknown"):
            return "unknown", None
        return outcome, got.get("reason") if isinstance(got.get("reason"), str) else None

    def _drop_conn(self, entry: dict[str, Any]) -> None:
        """A connection in an unknown state (a framing error mid-answer) is never reused."""
        conn = self.conns.pop(entry["workspace_id"], None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _deposit_status(self, entry: dict[str, Any], deposit_id: str) -> tuple[str, str | None]:
        try:
            got = self._call(entry, "grant.deposit_status", {"deposit_id": deposit_id})["data"]
        except Exception:  # noqa: BLE001 - a ClickError or anything the host client raises: still unknown
            self._drop_conn(entry)
            return "unknown", None
        outcome = got.get("deposit")
        if outcome not in ("confirmed", "rejected"):
            return "unknown", None
        return outcome, got.get("reason") if isinstance(got.get("reason"), str) else None

    def _replay(self, entry: dict[str, Any], rec: dict[str, Any], view_digest: str) -> dict[str, Any]:
        """Answer from a durable record: the cached result, or the identical events and idem resent. Another
        ``view_digest`` than the recorded one is ``already_signed``: a request is never signed twice."""
        if rec["view_digest"] != view_digest:
            raise refused("already_signed", "this request was already signed for another review; prepare a new one")
        key = (rec["workspace_id"], rec["request_id"])
        if is_claim(rec):  # claimed, never completed
            held = self.approvals.get(key)
            if held is None or held.signed is None or held.idem != rec["idem"]:
                # a crash or failure sat between the claim and the completion: unknown whether a signature exists
                raise refused(
                    "record_unavailable",
                    "the request was claimed but never recorded as signed; this signer does not sign it again",
                )
            try:  # signed in this process: the identical events, once the completion is durable
                rec = self.records.complete(rec, held.signed)
            except RecordError as exc:
                raise refused("record_unavailable", f"{exc}; nothing was submitted") from None
        if rec["result"] is not None:  # a repeated sign: the recorded answer
            return {"result": rec["result"], "duplicate": True}
        approval = self.approvals.get(key)
        if approval is None:  # after a restart: the record alone, enough to resend (never to sign)
            approval = Approval(
                workspace_id=rec["workspace_id"],
                request_id=rec["request_id"],
                op=rec["op"],
                kind="click",
                binding=b"",
                prepared={},
                gen=-1,
                deadline=0.0,
                expires_at=rec["expires_at"],
                files=None,
                view_digest=rec["view_digest"],
            )
        approval.signed, approval.idem = rec["events"], rec["idem"]
        return self._submit(entry, approval, rec)

    def _rerecord(self, approval: Approval) -> None:
        """The record of a request signed in this process is gone (a cleanup, a restore): write it again from memory,
        best effort. The resend does not depend on it."""
        try:
            if self.records.get(approval.workspace_id, approval.request_id) is None:
                self.records.create(
                    approval.workspace_id,
                    approval.request_id,
                    view_digest=str(approval.view_digest),
                    op=approval.op,
                    idem=str(approval.idem),
                    events=list(approval.signed or []),
                    expires_at=approval.expires_at,
                )
        except RecordError:
            self._note("record_failed", workspace=approval.workspace_id, request=approval.request_id)

    def _submit(self, entry: dict[str, Any], approval: Approval, rec: dict[str, Any] | None) -> dict[str, Any]:
        args = {"request_id": approval.request_id, "events": approval.signed}
        self._note("submit", workspace=approval.workspace_id, request=approval.request_id, op=approval.op)
        try:
            res = self._call(entry, "human.submit", args, idem=approval.idem)
        except ClickError as exc:
            if exc.code == "host.unavailable":
                try:  # one more time on a fresh connection: the identical events and idem
                    res = self._call(entry, "human.submit", args, idem=approval.idem)
                except ClickError as again:
                    if again.code == "host.unavailable":
                        self._note("uncertain", workspace=approval.workspace_id, request=approval.request_id)
                        raise refused("uncertain", "the submit's outcome is unknown; sign again to retry") from None
                    exc = again
                else:
                    return self._done(approval, rec, res)
            if exc.code in ("idem.conflict", "idem.expired", "outcome.unknown"):
                return self._recover(entry, approval, exc)
            raise  # the signed events stay: a retry resends them, the request is never signed twice
        return self._done(approval, rec, res)

    def _done(self, approval: Approval, rec: dict[str, Any] | None, res: dict[str, Any]) -> dict[str, Any]:
        approval.result = res
        try:  # after the commit nothing may escape: the record keeps the events, a repeated sign resends them
            if rec is None:
                rec = self.records.get(approval.workspace_id, approval.request_id)
            if rec is not None:
                self.records.settle(rec, res)
        except Exception:  # noqa: BLE001
            self._note("record_failed", workspace=approval.workspace_id, request=approval.request_id)
        self._note("committed", workspace=approval.workspace_id, request=approval.request_id, op=approval.op)
        return {"result": res, "duplicate": bool(res.get("duplicate"))}

    def _recover(self, entry: dict[str, Any], approval: Approval, exc: ClickError) -> dict[str, Any]:
        try:
            found = self._call(entry, "human.pending", {"request_id": approval.request_id})["data"]["requests"]
        except ClickError:
            found = []
        state = found[0]["state"] if found else "unknown"
        self._note("uncertain", workspace=approval.workspace_id, request=approval.request_id, code=exc.code)
        raise refused("uncertain", f"{exc.code}; the request is {state}") from None

    def cancel(self, args: Mapping[str, Any]) -> dict[str, Any]:
        entry, approval = self._approval(args)
        self._drop(approval)  # first: no sign can start on it any more
        res = self._call(entry, "human.cancel", {"request_id": approval.request_id})
        self._note("cancelled", workspace=approval.workspace_id, request=approval.request_id)
        return {"state": res["data"]["state"]}

    def status(self, args: Mapping[str, Any]) -> dict[str, Any]:
        from orch import __version__ as core
        from orch.custody.select import status

        out = [self._row(e, status) for e in self._workspaces()]
        return {"core": str(core), "contract": CONTRACT, "features": list(FEATURES), "workspaces": out}

    def _row(self, e: dict[str, Any], status: Callable[[Path, str], dict[str, Any]] | None = None) -> dict[str, Any]:
        if status is None:
            from orch.custody.select import status
        wid = e["workspace_id"]
        st = status(self.state, wid)
        row = {
            "workspace_id": wid,
            "prefix": str(e.get("prefix")),
            "custody": st["custody"],
            "device": st["device"],
            "enabled": bool(self._enabled(e)),
        }
        problem = st.get("problem") or self.problems.get(wid)
        if problem is None and st["custody"] != "convenience":
            problem = "not_convenience"
        if problem is None and not row["enabled"]:
            problem = "disabled"
        row["available"] = problem is None
        if problem is not None:
            row["problem"] = str(problem)[:200]
        return row

    # -- the custody switch (§0.8.6 item 3a, owner decision 15 Oct 2026) ---------------------------------------------
    def _custody_budget(self) -> None:
        """Wrong passphrases at :meth:`custody_on`: at most ``CUSTODY_TRIES`` per ``CUSTODY_WINDOW`` seconds in this
        signer. It slows guessing through a stolen dashboard session; it is no bound against code running as the
        person, which can read the passphrase key file itself."""
        now = self.mono()
        while self.custody_misses and self.custody_misses[0] <= now - CUSTODY_WINDOW:
            self.custody_misses.popleft()
        if len(self.custody_misses) >= CUSTODY_TRIES:
            wait = self.custody_misses[0] + CUSTODY_WINDOW - now
            raise ClickError(
                "rate.limited",
                f"at most {CUSTODY_TRIES} wrong passphrases per {int(CUSTODY_WINDOW)} s; wait, or use the terminal",
                retry_after_ms=max(1, int(wait * 1000)),
                retryable=True,
            )

    @staticmethod
    def _passphrase(args: Mapping[str, Any], *, required: bool) -> str | None:
        pw = args.get("passphrase")
        if pw is None and not required:
            return None
        if type(pw) is not str or not 0 < len(pw) <= PASSPHRASE_MAX:
            raise ClickError("invalid.input", "passphrase is text")
        return pw

    def custody_on(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Click-to-sign on, from the dashboard: the device key moves into convenience custody with the passphrase the
        person typed (the one factor that someone who takes over the dashboard does not have), then the workspace's
        flag is set, both under the flag lock (§0.8.6 item 8). The same refusals as ``orch host convenience on`` and
        ``orch keys custody convenience``. The passphrase is never logged or returned."""
        from orch.custody import select
        from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound, WrongPassphrase
        from orch.host import convenience
        from orch.host.errors import HostError

        entry = self._entry(args.get("workspace_id"))
        pw = self._passphrase(args, required=True)
        self._custody_budget()
        wid, uid = entry["workspace_id"], int(entry["host_uid"])
        try:
            with convenience.workspace_lock(self.state, wid, uid):
                waiting = convenience.pending_handoffs(self.state, wid, uid, clock=self.wall)
                if waiting:
                    raise ClickError(
                        "sign.click_only",
                        f"{len(waiting)} terminal hand-off request(s) are pending for this workspace: let them expire "
                        "(10 minutes), then switch click-to-sign on",
                    )
                if select.status(self.state, wid)["custody"] != "convenience":
                    try:
                        select.convert_to_convenience(
                            self.state, wid, prefix=str(entry.get("prefix") or ""), passphrase=pw
                        )
                    except WrongPassphrase:
                        self.custody_misses.append(self.mono())
                        raise ClickError(
                            "custody.wrong_passphrase", "the passphrase was refused; nothing changed"
                        ) from None
                convenience.set_enabled(self.state, wid, True, uid)
        except HostError as exc:  # lock.busy, custody.unsafe_mode
            raise ClickError(exc.code, exc.message, retryable=exc.code == "lock.busy") from None
        except KeyNotFound as exc:
            raise ClickError("custody.no_key", str(exc)) from None
        except (BackendUnavailable, CustodyError) as exc:
            raise ClickError("custody.unavailable", str(exc)) from None
        self.problems.pop(wid, None)
        self._note("custody_on", workspace=wid)
        return self._row(entry)

    def custody_off(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Click-to-sign off, from the dashboard: the flag goes off at once (no factor: it only takes authority away).
        With ``passphrase`` the key also moves back under that new passphrase (``orch keys custody passphrase
        --in-place``; it must pass the strength rules); without it the key stays in the key store until then."""
        from orch.custody import select
        from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound
        from orch.custody.strength import check_strength
        from orch.host import convenience
        from orch.host.errors import HostError

        entry = self._entry(args.get("workspace_id"))
        pw = self._passphrase(args, required=False)
        wid, uid = entry["workspace_id"], int(entry["host_uid"])
        if pw is not None:
            try:
                check_strength(pw, (str(entry.get("prefix") or ""),))
            except CustodyError as exc:
                raise ClickError("custody.weak_passphrase", str(exc)) from None
        try:
            with convenience.workspace_lock(self.state, wid, uid):
                convenience.set_enabled(self.state, wid, False, uid)
            if pw is not None:
                select.downgrade_in_place(self.state, wid, passphrase=pw)
        except HostError as exc:
            raise ClickError(exc.code, exc.message, retryable=exc.code == "lock.busy") from None
        except KeyNotFound as exc:
            raise ClickError("custody.no_key", str(exc)) from None
        except (BackendUnavailable, CustodyError) as exc:  # e.g. another custody change is unfinished
            raise ClickError("custody.unavailable", str(exc)) from None
        self._note("custody_off", workspace=wid, moved_back=pw is not None)
        return self._row(entry)

    # -- bookkeeping ---------------------------------------------------------------------------------------------------
    def _drop(self, approval: Approval) -> None:
        self.approvals.pop((approval.workspace_id, approval.request_id), None)

    def _sweep(self) -> None:
        for key, a in list(self.approvals.items()):
            if self._expired(a) or a.result is not None:
                self.approvals.pop(key, None)
        # ``_attempted`` is deliberately not swept: it lives as long as the process (see its definition).

    def close(self) -> None:
        for c in self.conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self.conns.clear()

    def handle(self, op: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(args, Mapping):
            raise ClickError("invalid.input", "args is an object")
        fn = {
            "prepare": self.prepare,
            "view": self.view,
            "sign": self.sign,
            "cancel": self.cancel,
            "custody_on": self.custody_on,
            "custody_off": self.custody_off,
        }.get(op)
        if op == "status":
            return self.status(args)
        if fn is None:
            raise ClickError("invalid.input", f"unknown op {op!r}")
        try:
            return fn(args)
        except ClickError as exc:  # every refusal leaves a line: the static code and the validated ids only
            self._note(
                "uncertain" if exc.reason == "uncertain" else "refused",
                workspace=args.get("workspace_id"),
                request=args.get("request_id"),
                op=args.get("op"),
                code=exc.code if exc.reason is None else f"{exc.code}.{exc.reason}",
            )
            raise


def _epoch(stamp: str) -> float:
    import calendar

    try:
        return float(calendar.timegm(time.strptime(stamp.replace(".000", "")[:20], "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0
