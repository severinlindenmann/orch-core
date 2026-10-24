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

import base64
import binascii
import os
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orch import canon

from . import sealed
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
#: the wrong-passphrase record of the switch, in the person's custody root (it survives a signer restart: Opus M1)
CUSTODY_MISSES = "click-custody.json"
PASSPHRASE_MAX = 1024
REQUEST_LIFETIME = 600.0  # §0.8.1: a request lives 10 minutes
HUMAN_OPS_V1 = ("human.prepare", "human.get", "human.submit", "human.pending", "human.cancel")


#: the readers-condition reasons that mean "a reader is below level 1" (§0.8.6 item 9.9): ``status`` shows
#: ``readers_old`` for the workspace until a call succeeds again (H12d-2, 9.11)
READERS_OLD = frozenset({"relay_paired", "other_member", "phone_device"})

GRANT_ID = re.compile(r"^gr_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


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
    #: #1198 C7: prepared to show the grant secret once in the page, sealed to the page's key (``reveal_key`` at sign)
    reveal: bool = False
    #: the sealed envelope of this approval's secret and the page key it is sealed to: held (ciphertext only, useless
    #: without the page's private key) so a lost answer or a retried submit delivers the same envelope, never a second
    #: sealing; the secret itself is never kept
    reveal_pk: str | None = None
    reveal_env: dict[str, Any] | None = None
    deposit_result: dict[str, Any] | None = None
    #: R3 part B: the bytes of the pasted screenshots of a sequence by name, held HERE (verified against the intent)
    images: dict[str, bytes] | None = None
    image_digests: tuple[str, ...] = ()


def _new_idem() -> str:
    from orch.host.control_client import new_idem

    return new_idem()


SHA = re.compile(r"^sha256:[0-9a-f]{64}$")


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
        sessions: Any = None,
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
        #: R3 part B: pasted screenshots uploaded by ``stash`` (workspace, digest) -> {buf, size, done, at}
        self.stash_: dict[tuple[str, str], dict[str, Any]] = {}
        self.conns: dict[str, Any] = {}
        self.busy = False
        #: page keys a grant secret was sealed to in this process: one key, one sign call (sealed-field contract §3)
        self._reveal_keys: set[str] = set()
        #: ``(workspace_id, request_id)`` whose signing started in this process: defence in depth against a claim
        #: record deleted from outside after a failed or interrupted signature (the claim file alone is not trusted)
        #: Kept for the whole signer process lifetime, never pruned: memory is bounded by the sign budget (30/min,
        #: 600/h) and an entry is one tiny key, while a pruned marker would reopen the second-signature window.
        self._attempted: set[tuple[str, str]] = set()
        self.log = log
        self.records = records if records is not None else Records(self.state, wall=wall)
        self.records.prune()  # retention runs at start and at prepare, never on the sign path
        from orch.host.sequence import Sessions

        #: the durable sessions of sequences (one review, N signatures; batch 11), beside the sign records
        self.sessions = sessions if sessions is not None else Sessions(self.state, wall=wall)
        #: the last connection or custody problem per workspace, for ``status`` (it loads no key and connects nowhere)
        self.problems: dict[str, str] = {}

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

    def _risk_ack(self, entry: dict[str, Any]) -> str | None:
        """B6: when the risk notice was acknowledged at enable time (``None``: enabled before B6, or off)."""
        from orch.host.convenience import risk_ack

        try:
            return risk_ack(self.state, entry["workspace_id"], int(entry["host_uid"]))
        except (OSError, ValueError):
            return None

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
            reveal=approval.reveal,
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
        seq = args.get("sequence")
        images: dict[str, bytes] | None = None
        send_files: dict[str, Any] | None = None
        if seq is not None:
            self._sequence_ok(entry, op, inner, seq, deposit, files)
            images = self._images_for(entry, seq)
            if images and op == "artifact.add":  # an edit whose first step adds a screenshot
                first = images.get(str(inner.get("name")))
                if first is None:
                    raise ClickError("invalid.input", "the first step names a screenshot the review does not carry")
                files = {"path": {"b64": base64.b64encode(first).decode()}}
                send_files = self._host_files(entry, first)
        reveal = op == "grant" and not deposit
        reveal_key: str | None = None
        if args.get("reveal_key") is not None and not reveal:
            raise ClickError("invalid.input", "only a grant shown in the page takes reveal_key")
        if deposit:  # v1.6 (D-P2-27): the one click grant, handed straight to the runner, never shown
            if op != "grant":
                raise ClickError("invalid.input", "deposit is for grant only")
            inner = self._deposit_args(entry, inner)
        elif reveal:  # #1198 C7: the secret is shown once in the page, sealed to a key it sends at sign
            if seq is not None or files:
                raise ClickError("invalid.input", "a grant takes no sequence and no files")
            inner = self._reveal_args(inner)
            reveal_key = self._reveal_key_arg(args)  # bound into the review: the page sends it at prepare
            if reveal_key is None or reveal_key in self._reveal_keys:
                raise ClickError("invalid.input", "a shown grant takes a new reveal_key at prepare")
        elif op_class(op, args=inner) == "terminal":
            raise refused("terminal_only", f"{op} is signed in the terminal (orch sign CODE)")
        if len(self.approvals) >= MAX_APPROVALS:
            self._sweep()
            if len(self.approvals) >= MAX_APPROVALS:
                raise ClickError("rate.limited", "too many open sign requests", retry_after_ms=10_000, retryable=True)
        self.records.prune()
        _, sel = self._convenience(entry)
        device = sel["device"]
        body = {"op": op, "args": inner, "signer": {"device": device, "auth": "convenience"}}
        if seq is not None:
            body["sequence"] = seq
        prepared = self._call(entry, "human.prepare", body, idem=idem, files=send_files or files or None)
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
            reveal=reveal,
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
                reveal=reveal,
                reveal_pk=reveal_key,
                images=images or None,
                image_digests=tuple(sorted({canon.artifact_digest(b) for b in images.values()})) if images else (),
            )
        approval = self.approvals[key]
        if approval.deposit != deposit or approval.reveal != reveal or approval.reveal_pk != reveal_key:
            raise ClickError("idem.conflict", "this idem named a request prepared in another mode")
        self._note("prepared", workspace=entry["workspace_id"], request=rid, op=op)
        return {"request_id": rid, "expires_at": approval.expires_at, "op": op, "confirm": approval.kind}

    # -- pasted screenshots (R3 part B) --------------------------------------------------------------------------
    STASH_FILES = 12
    STASH_BYTES = 40 * 1024 * 1024
    STASH_LIFETIME = 900.0  # seconds a pasted file waits for its prepare

    def stash(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """One chunk of a pasted screenshot. The signer holds the bytes (it checks them against the digest the person
        reviewed and against :mod:`orch.host.imagecheck`) so the host never decides what the person signs. Chunks arrive
        in order from offset 0; a chunk already held is accepted without effect when identical."""
        from orch.host import imagecheck
        from orch.host.blobs import CHUNK_MAX

        entry = self._entry(args.get("workspace_id"))
        digest, size, offset, b64, last = (args.get(k) for k in ("sha256", "size", "offset", "b64", "last"))
        if type(digest) is not str or not SHA.fullmatch(digest):
            raise ClickError("invalid.input", "stash takes the sha256 of the picture")
        if type(size) is not int or not 1 <= size <= imagecheck.IMAGE_MAX:
            raise ClickError("invalid.input", "a screenshot is 1 byte to 5 MiB")
        if type(offset) is not int or offset < 0 or type(last) is not bool or type(b64) is not str:
            raise ClickError("invalid.input", "stash takes offset, b64 and last")
        try:
            chunk = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            raise ClickError("invalid.input", "a chunk is not base64") from None
        if len(chunk) > CHUNK_MAX:
            raise ClickError("invalid.input", "a chunk is at most 768 KiB")
        now = self.mono()
        self._sweep()  # an expired approval no longer holds pictures: the stash is the one place their bytes count
        pinned = self._pinned()
        for k, v in list(self.stash_.items()):
            if k in pinned:
                v["at"] = now  # a prepared review still names this file: it waits as long as the review does
            elif now - v["at"] > self.STASH_LIFETIME:
                del self.stash_[k]
        key = (entry["workspace_id"], digest)
        held = self.stash_.get(key)
        if held is None:
            if offset != 0:
                raise ClickError("invalid.input", "a picture starts at offset 0")
            if (
                len(self.stash_) >= self.STASH_FILES
                or sum(v["size"] for v in self.stash_.values()) + size > self.STASH_BYTES
            ):
                raise ClickError("rate.limited", "too many pictures are waiting to be signed", retryable=True)
            held = self.stash_[key] = {"buf": bytearray(), "size": size, "done": False, "at": now}
        if held["size"] != size:
            raise ClickError("invalid.input", "the size of this picture changed")
        if held["done"]:
            return {"state": "complete"}
        end = offset + len(chunk)
        if offset < len(held["buf"]):  # a resend: only identical bytes are accepted, with no effect
            if end > len(held["buf"]) or bytes(held["buf"][offset:end]) != chunk:
                raise ClickError("invalid.input", "a chunk differs from the one held")
        elif offset != len(held["buf"]) or end > size:
            raise ClickError("invalid.input", "chunks arrive in order")
        else:
            held["buf"] += chunk
        if last:
            data = bytes(held["buf"])
            if len(data) != size or canon.artifact_digest(data) != digest:
                del self.stash_[key]
                raise ClickError("invalid.input", "the picture does not match its digest")
            try:
                imagecheck.check(data)
            except imagecheck.ImageRefused as exc:
                del self.stash_[key]
                raise ClickError("invalid.input", f"screenshot refused: {exc}") from None
            held["done"] = True
            held["data"] = data
            held["buf"] = bytearray()  # the bytes live once, as ``data``
            return {"state": "complete"}
        return {"state": "partial", "held": len(held["buf"])}

    def _pinned(self) -> set[tuple[str, str]]:
        """The stash entries a live prepared approval still needs (their bytes count once, in the stash)."""
        return {
            (a.workspace_id, d)
            for a in self.approvals.values()
            if a.images and not self._expired(a)
            for d in a.image_digests
        }

    def _images_for(self, entry: dict[str, Any], seq: Mapping[str, Any]) -> dict[str, bytes] | None:
        """The bytes of the intent's screenshots by name, from the stash: each complete, checked and equal to the
        digest and size of the intent. ``None`` for an intent without images."""
        from orch.host import sequence

        try:
            intent = sequence.validate_intent(seq.get("intent"))
        except sequence.SequenceInvalid as exc:
            raise ClickError("invalid.input", f"sequence: {exc}") from None
        wanted = intent.get("images") or ()
        out: dict[str, bytes] = {}
        for img in wanted:
            held = self.stash_.get((entry["workspace_id"], img["sha256"]))
            if held is None or not held["done"]:
                raise ClickError("invalid.input", f"the screenshot {img['name']} was not uploaded to the signer")
            data = held["data"]  # the one bytes object (no copy: the approval shares it with the stash)
            if canon.artifact_digest(data) != img["sha256"] or len(data) != img["bytes"]:
                raise ClickError("invalid.input", f"the screenshot {img['name']} is not the file in the review")
            held["at"] = self.mono()
            out[img["name"]] = data
        return out or None

    def _host_files(self, entry: dict[str, Any], data: bytes) -> dict[str, Any]:
        """``files`` of one ``human.prepare``: inline up to 512 KiB, else a blob this signer uploads itself."""
        from orch.host.blobs import INLINE_MAX
        from orch.hostclient import upload

        if len(data) <= INLINE_MAX:
            return {"path": {"b64": base64.b64encode(data).decode()}}

        class _Conn:
            @staticmethod
            def call(op: str, args: dict[str, Any], auth: Any = None) -> dict[str, Any]:
                return self._call(entry, op, args)

        return {"path": {"blob": upload(_Conn(), data, None)}}

    def _sequence_kind(self, entry: dict[str, Any], intent: Mapping[str, Any]) -> str:
        """The confirmation of a sequence (always ``click``, else ``terminal_only``). Its own rule, not part of any
        confirmation class: a sequence that ends in a grant to the runner needs dashboard execution on this machine."""
        from orch.host import sequence

        if sequence.confirm_kind(intent) == "terminal":
            raise refused("terminal_only", "this sequence needs more than a click; use the terminal (orch sign CODE)")
        if sequence.has_grant(intent) and not self._execution(entry):
            raise refused("terminal_only", "dashboard execution is off for this workspace on this machine")
        return "click"

    def _sequence_ok(
        self, entry: dict[str, Any], op: str, inner: dict[str, Any], seq: Any, deposit: bool, files: Any
    ) -> None:
        """A sequence (one review, N signatures, batch 11) starts with ``new`` and nothing else; the confirmation of
        the whole is a click (a grant to the runner last also needs dashboard execution on this machine).
        A terminal step means the terminal."""
        from orch.host import sequence

        if deposit or files or not isinstance(seq, dict) or seq.get("step") != 0:
            raise ClickError("invalid.input", "a sequence starts at its first step, without files or a deposit")
        try:
            intent = sequence.validate_intent(seq.get("intent"))
        except sequence.SequenceInvalid as exc:
            raise ClickError("invalid.input", f"sequence: {exc}") from None
        if op != sequence.steps(intent)[0]["op"]:  # a new ticket starts with new, an edit with its first change
            raise ClickError("invalid.input", "a sequence starts with the first op of its intent")
        self._sequence_kind(entry, intent)

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
        elif approval.images:
            artifacts = "checked"  # the pasted screenshots of a sequence: this signer holds the bytes and compared them
        return views.build(
            prepared,
            self._names(entry),
            device=device,
            expires_at=approval.expires_at,
            artifacts=artifacts,
            deposit=approval.deposit,
            reveal=approval.reveal_pk,
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
        reveal_key = self._reveal_key_arg(args)  # syntax only, before anything else; whether one is needed: below
        rec = self._record(entry["workspace_id"], rid)  # first, before the registry: it survives a restart
        if rec is not None:
            res = self._deposited(entry, rec["op"], rid, self._replay(entry, rec, vd))
            held = self.approvals.get((entry["workspace_id"], rid))
            return res if held is None else self._revealed(held, reveal_key, res)
        entry, approval = self._approval(args)
        if approval.signed is not None:  # the in-memory guard stays, also when the record was lost
            if vd != approval.view_digest:
                raise refused("already_signed", "this request was already signed for another review")
            if approval.result is not None:
                res = self._deposited(entry, approval.op, rid, {"result": approval.result, "duplicate": True})
            else:
                self._rerecord(approval)
                # the identical events and idem, never a new signature
                res = self._deposited(entry, approval.op, rid, self._submit(entry, approval, None))
            return self._revealed(approval, reveal_key, res)
        if (approval.workspace_id, approval.request_id) in self._attempted:
            # signing started here, and neither a record nor cached events exist: the claim was removed from outside
            raise refused(
                "record_unavailable",
                "signing this request was attempted but no record of it exists; this signer does not sign it again",
            )
        if self._expired(approval):
            self.approvals.pop((approval.workspace_id, approval.request_id), None)
            raise refused("expired", "the request expired; prepare it again")
        if approval.reveal and reveal_key != approval.reveal_pk:  # the key approved in the review, nothing else
            raise ClickError("invalid.input", "reveal_key is not the key this review was made for")
        if not approval.reveal and reveal_key is not None:
            raise ClickError("invalid.input", "only a grant shown in the page takes reveal_key")
        if "sealed" in args:  # no review asks for a typed secret yet (C10, C12, C13 and C16 add them)
            raise ClickError("invalid.input", "this review asks for no sealed value")
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
        if approval.deposit and not self._execution(entry):  # switched off since prepare: nothing is claimed or signed
            raise refused("terminal_only", "dashboard execution is off for this workspace on this machine")
        stored = approval.prepared  # the immutable inputs validated at prepare (== prepared, checked above)
        if (stored.get("material") or {}).get("sequence") is not None:
            return self._sign_sequence(entry, approval, backend, sel, stored)
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
        if secret is not None and not (approval.deposit or approval.reveal):  # refused earlier; never near the channel
            raise refused("terminal_only", "a request that draws a secret is signed in the terminal")
        if approval.reveal:  # sealed to the page's key before anything else happens: no clear copy leaves this frame
            self._seal(approval, reveal_key, secret)
        approval.signed, approval.idem = signed.events, idem  # in memory at once: a retry resends, never re-signs
        try:
            try:  # durable before the signature leaves: a crash after this resends these events, never signs again
                rec = self.records.complete(claim, signed.events)
            except RecordError as exc:  # nothing left the signer; a sign in this process retries the completion
                raise refused("record_unavailable", f"{exc}; nothing was submitted") from None
            self._note("signed", workspace=approval.workspace_id, request=approval.request_id)
            res = self._replay(entry, rec, approval.view_digest)  # the commit first, then the hand-over
            return self._revealed(approval, reveal_key, self._deposited(entry, approval.op, rid, res, secret))
        finally:
            del secret, signed  # best effort: Python cannot wipe a str; nothing keeps a reference to it

    def _sign_sequence(
        self, entry: dict[str, Any], approval: Approval, backend: Any, sel: Any, stored: dict[str, Any]
    ) -> dict[str, Any]:
        """One click signs every step of a sequence (batch 11): the engine of :mod:`orch.host.sequence`, on this
        signer's own host connection and its own convenience key. The session record is the durable claim (a step is
        claimed, then signed, then landed; never signed twice); the checks run HERE, in this process, never in the
        dashboard server. A sequence that ends early leaves what landed as a visible draft."""
        from orch.host import sequence
        from orch.host import signer as host_signer

        wid, person, device = approval.workspace_id, self._person(entry), sel["device"]
        self._attempted.add((wid, approval.request_id))
        intent = sequence.validate_intent(stored["material"]["sequence"]["intent"])

        def prepare(
            op: str, args: dict[str, Any], meta: dict[str, Any], files: dict[str, bytes] | None = None
        ) -> dict[str, Any]:
            body = {"op": op, "args": args, "signer": {"device": device, "auth": "convenience"}, "sequence": meta}
            sent = self._host_files(entry, files["path"]) if files else None
            return canon.loads_strict(canon.dumps(self._call(entry, "human.prepare", body, files=sent)["data"]))

        box: dict[str, Any] = {}

        def check(prepared: dict[str, Any]) -> None:
            # every §0.8.1 check; what each step may be is the intent's (check_step), including a last grant, which a
            # lone click never signs
            try:
                host_signer.check(prepared, workspace_id=wid, person=person, device=device, auth="convenience")
            except host_signer.SignerRefused as exc:
                raise refused("check_failed", str(exc)) from None
            # in ADDITION: the click rules of every single request (event count, clean worker checks, the grant's
            # terminal-only gate: only the runner grant of deposit mode passes, as the sequence's last step)
            views.check_prepared(
                prepared,
                workspace_id=wid,
                person=person,
                device=device,
                files=None,
                deposit=prepared.get("op") == "grant",
            )

        def sign(prepared: dict[str, Any]) -> list[dict[str, Any]]:
            signed = host_signer.sign(prepared, backend=backend, key_id=KEY_ID, workspace_id=wid)
            if signed.secret is not None:  # the runner grant (last): this frame only, handed over after its commit
                box["secret"] = signed.secret
            return signed.events

        def submit(rid: str, events: list[dict[str, Any]], idem: str) -> dict[str, Any]:
            return self._call(entry, "human.submit", dict(request_id=rid, events=events), idem=idem)

        def head(key: str) -> dict[str, Any]:
            data = self._call(entry, "ticket.get", {"ref": key})["data"]
            return {**data["doc"]["head"], "tail": (data.get("log") or [None])[-1]}

        def cancel(rid: str) -> None:
            self._call(entry, "human.cancel", {"request_id": rid})

        port = sequence.Port(
            prepare=prepare,
            check=check,
            sign=sign,
            submit=submit,
            new_idem=_new_idem,
            head=head,
            cancel=cancel,
            budget=self.budget.reserve,
        )
        kind = self._sequence_kind(entry, intent)  # re-asserted here: what is signed is what this click may sign
        if approval.kind != kind:  # the approval recorded at prepare must be the class the host-side intent has now
            raise refused("terminal_only", "this sequence needs more than a click; use the terminal (orch sign CODE)")
        driver = sequence.Driver(
            port,
            self.sessions,
            workspace_id=wid,
            person=person,
            intent=intent,
            now=self.wall,
            images=approval.images,
        )
        try:
            live = driver.resumable()
        except sequence.SessionError as exc:
            raise refused("record_unavailable", f"{exc}; nothing was signed") from None
        if live is not None:
            # a run of this very review is half done: a click shows the create review, never a resume; the terminal does
            if intent["kind"] == sequence.ADDON:  # no ticket
                raise refused(
                    "resumable",
                    "Turning this add-on on was started from this review and is not finished. Finish it in your "
                    "terminal (orch sign); nothing is signed here.",
                )
            key = live["ticket"]["key"] if live.get("ticket") else "the ticket"
            raise refused(
                "resumable",
                f"{key} was started from this review and is not finished. Finish it in your terminal (orch sign) "
                "or on the ticket page; nothing is signed here.",
            )
        try:
            out = driver.run(stored)
        except sequence.Aborted as exc:
            self._note("sequence_aborted", workspace=wid, request=approval.request_id, landed=exc.landed)
            where = ""
            if exc.landed:
                try:
                    held = self.sessions.get(wid, driver.digest)
                except sequence.SessionError:
                    held = None
                name = f" {held['ticket']['key']}" if held and held.get("ticket") else ""
                where = f" {exc.landed} of {exc.total} signatures landed; the ticket{name} stays a draft."
                if intent["kind"] == sequence.ADDON:  # no ticket: the settings stay and the add-on stays off
                    where = (
                        f" {exc.landed} of {exc.total} signatures landed; the settings stay and the add-on stays off."
                    )
            if exc.resumable:
                unfinished = where.replace(" stays a draft", " is not finished")
                if intent["kind"] == sequence.ADDON:  # the last answer was lost: whether it is on is not known
                    unfinished = (
                        f" {exc.landed} of {exc.total} signatures landed; whether the add-on is on is not known yet."
                    )
                page = "" if intent["kind"] == sequence.ADDON else " or on the ticket page"
                raise refused(
                    "resumable",
                    f"The host did not confirm the last signature.{unfinished} "
                    f"Finish it in your terminal (orch sign){page}; nothing more is signed here.",
                ) from None
            raise refused("stopped", f"{exc.reason}.{where}") from None
        except sequence.SessionError as exc:
            raise refused("record_unavailable", f"{exc}; nothing more is signed") from None
        last = out["results"][-1]
        approval.signed, approval.idem = ["sequence"], None  # in memory: a repeated sign answers, never re-signs
        approval.result = last
        self._note("committed", workspace=wid, request=approval.request_id, op=approval.op, steps=out["landed"])
        answer: dict[str, Any] = {"result": last, "duplicate": False}
        secret = box.pop("secret", None)
        if secret is not None:  # the grant is signed and committed: now, and only now, to the runner
            did = out["requests"][-1][3:]
            outcome, reason = self._hand_over(entry, did, secret)
            if outcome == "unknown":
                outcome, reason = self._deposit_status(entry, did)
            answer |= {"deposit": outcome, "deposit_id": did, "grant": secret.partition(".")[0]}
            if outcome == "rejected":
                answer["deposit_reason"] = reason if reason in DEPOSIT_REASONS else "other"
            del secret
        return answer

    def _deposit_args(self, entry: dict[str, Any], inner: dict[str, Any]) -> dict[str, Any]:
        """A deposit grant's arguments: ``hours`` 1..168 (the workspace grant_hours; default 8) and
        ``scope``; the signer sets ``label:"runner"``
        and ``verbs:"agent"`` itself. Refused (``terminal_only``) unless this machine has dashboard execution on."""
        unknown = set(inner) - {"hours", "scope", "label", "verbs", "runner", "extend"}
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
        # runner:true makes the op itself a runner grant (label runner, workspace grant_hours cap, verbs agent, no hint)
        out: dict[str, Any] = {"hours": hours, "scope": scope, "verbs": "agent", "label": "runner", "runner": True}
        if "extend" in inner:  # B8: the grant this one replaces; the op checks it is yours and live
            if not (isinstance(inner["extend"], str) and GRANT_ID.fullmatch(inner["extend"])):
                raise ClickError("invalid.input", "extend is a grant id")
            out["extend"] = inner["extend"]
        return out

    def _deposited(
        self, entry: dict[str, Any], op: str, rid: str, res: dict[str, Any], secret: str | None = None
    ) -> dict[str, Any]:
        """A deposit grant's answer gains ``deposit``, ``deposit_id`` (the request's ULID) and ``grant``. With the
        secret (this very signature): ``grant.deposit`` on the signer's own connection, after the commit. Without
        it (a repeated ``sign``, a restart): the settled outcome, else ``grant.deposit_status`` once."""
        wid = entry["workspace_id"]
        approval = self.approvals.get((wid, rid))
        if op != "grant" or (approval is not None and approval.reveal):  # a reveal grant has no runner hand-over
            return res
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

    # -- the grant secret shown once in the page (#1198 C7, sealed-field contract section 3) -------------------------
    @staticmethod
    def _reveal_key_arg(args: Mapping[str, Any]) -> str | None:
        """``reveal_key``: the page's P-256 public key (65-byte point, base64url), or absent. Syntax only."""
        key = args.get("reveal_key")
        if key is None:
            return None
        try:
            sealed.decode_public(key, "reveal_key")
        except sealed.SealedError:
            raise ClickError("invalid.input", "reveal_key is a P-256 public key (base64url)") from None
        return key

    def _seal(self, approval: Approval, reveal_key: str | None, secret: str | None) -> None:
        """Seal the secret to the page's key, bound to this workspace, request and review. At most once per approval.
        Only the ciphertext is kept; the secret never leaves this frame. A failure here leaves the claim: the request
        fails closed, nothing was submitted."""
        if reveal_key is None or secret is None or approval.view_digest is None or approval.reveal_env is not None:
            raise refused("check_failed", "the secret cannot be sealed for this request")
        ctx = sealed.Context("grant.secret", approval.workspace_id, approval.request_id, approval.view_digest)
        try:
            env = sealed.seal(reveal_key, secret, ctx)
        except sealed.SealedError as exc:
            raise refused("check_failed", f"the secret cannot be sealed ({exc.reason})") from None
        self._reveal_keys.add(reveal_key)
        approval.reveal_env = env

    def _revealed(self, approval: Approval, reveal_key: str | None, res: dict[str, Any]) -> dict[str, Any]:
        """A reveal grant's answer gains ``reveal`` {purpose, envelope}: the envelope sealed at the signature, to the
        same key (a retry with another key, or a signer that restarted, has none: the secret is not kept anywhere)."""
        if not approval.reveal or approval.reveal_env is None:
            return res
        if reveal_key != approval.reveal_pk:
            raise ClickError("invalid.input", "reveal_key is not the key this request was sealed to")
        return {**res, "reveal": {"purpose": "grant.secret", "envelope": approval.reveal_env}}

    def _reveal_args(self, inner: dict[str, Any]) -> dict[str, Any]:
        """A shown grant's arguments: ``hours`` 1..168 (default 8), ``scope`` and an optional short ``label``; verbs are
        ``agent``. Not a runner grant (that one is the deposit mode)."""
        unknown = set(inner) - {"hours", "scope", "label"}
        hours, scope, label = inner.get("hours", 8), inner.get("scope", "workable"), inner.get("label")
        if unknown or type(hours) is not int or scope not in ("all", "workable"):
            raise ClickError("invalid.input", "a shown grant takes hours (a whole number), scope and a label")
        if not 1 <= hours <= DEPOSIT_MAX_HOURS:
            raise refused("terminal_only", f"a click grant lasts at most {DEPOSIT_MAX_HOURS} hours; use the terminal")
        out: dict[str, Any] = {"hours": hours, "scope": scope, "verbs": "agent"}
        if label is not None:
            if type(label) is not str or not 1 <= len(label) <= 40 or label.strip().lower() == "runner":
                raise ClickError("invalid.input", "label is 1 to 40 characters and is not runner")
            out["label"] = label
        return out

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
            "risk_ack": self._risk_ack(e),
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

    # -- the custody switch (§0.8.6 item 3a, owner decision 15 Oct 2026; fix-forward of the Opus review of #539) -----
    #
    # The invariant, enforced here and nowhere else: the dashboard turns click-to-sign on only by proving the
    # passphrase of a key that is in passphrase custody NOW, and it never chooses a passphrase. So:
    #   custody_on   passphrase custody            -> the conversion (the passphrase is the proof), then the flag
    #                convenience custody, flag on  -> nothing to do (idempotent)
    #                convenience custody, flag off -> refused: custody.terminal_needed (nothing to prove against)
    #                an unfinished change          -> refused: custody.unavailable
    #   custody_off  the flag goes off (no factor: it only takes authority away). V3-D31 narrows H2 for this path only:
    #                with a NEW passphrase, while the flag is ON at that moment (checked under the workspace lock: the
    #                session held one-click authority anyway), the key moves back to passphrase custody in the same
    #                locked step (select.turn_off_convenience). A passphrase with the flag already off or a custody
    #                change pending: custody.terminal_needed, nothing changed. Without a passphrase: flag only.
    def _misses_dir(self) -> int:
        from orch.custody.convenience import person_root, secure_dir

        root = person_root(self.state)
        root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # the person's state directory
        return secure_dir(root, (), create=True)

    def _misses(self) -> list[float]:
        """The recorded wrong passphrases (wall times) inside the window, from ``<person>/click-custody.json`` (0600,
        person-owned, :data:`CUSTODY_MISSES`). An unreadable record is replaced by a full window of misses now: it
        fails closed for one window instead of resetting the count, and not for longer."""
        import json

        from orch.custody.base import CustodyError, KeyNotFound
        from orch.custody.convenience import read_private

        now = self.wall()
        fd = self._misses_dir()
        try:
            try:
                doc = json.loads(read_private(fd, CUSTODY_MISSES))
                misses = doc["misses"]
                if doc.get("v") != 1 or type(misses) is not list or not all(type(t) is float for t in misses):
                    raise ValueError("shape")
            except KeyNotFound:
                return []
            except (CustodyError, ValueError, KeyError, TypeError):
                misses = [now] * CUSTODY_TRIES
                self._write_misses(fd, misses)
                return misses
        finally:
            os.close(fd)
        # a time ahead of the clock (it was set back) counts as now, and is written back as now once (Opus F2 on #578):
        # otherwise it would count as "now" again at every read and the wait would last until the clock catches up
        if any(t > now for t in misses):
            misses = [min(t, now) for t in misses]
            fd = self._misses_dir()
            try:
                self._write_misses(fd, misses)
            finally:
                os.close(fd)
        return sorted(t for t in misses if t > now - CUSTODY_WINDOW)[-CUSTODY_TRIES:]

    @staticmethod
    def _write_misses(fd: int, misses: list[float]) -> None:
        import json

        from orch.custody.convenience import write_private

        write_private(fd, CUSTODY_MISSES, json.dumps({"v": 1, "misses": misses}).encode("ascii"), replace=True)

    def _miss(self) -> None:
        misses = [*self._misses(), self.wall()][-CUSTODY_TRIES:]  # bounded: older ones no longer matter
        fd = self._misses_dir()
        try:
            self._write_misses(fd, misses)
        finally:
            os.close(fd)

    def _custody_budget(self) -> None:
        """Wrong passphrases at :meth:`custody_on`: at most ``CUSTODY_TRIES`` per ``CUSTODY_WINDOW`` seconds, recorded
        on disk so a signer restart (a crash, a dashboard ``stop``, a replaced hung signer) does not reset it. It
        locks out the dashboard path only; ``orch host convenience on`` and ``orch keys custody convenience`` in the
        terminal are not limited. It slows guessing through a stolen dashboard session; it is no bound against code
        running as the person, which can read the passphrase key file itself (or keep this path locked: it fails
        closed)."""
        from orch.custody.base import CustodyError

        try:
            misses = self._misses()
        except (CustodyError, OSError) as exc:
            raise ClickError("custody.unavailable", f"the passphrase attempt record is not usable: {exc}") from None
        if len(misses) >= CUSTODY_TRIES:
            wait = misses[0] + CUSTODY_WINDOW - self.wall()
            raise ClickError(
                "rate.limited",
                f"at most {CUSTODY_TRIES} wrong passphrases per {int(CUSTODY_WINDOW)} s here; wait, or switch it on in "
                "the terminal (orch keys custody convenience, orch host convenience on)",
                retry_after_ms=max(1, int(wait * 1000)),
                retryable=True,
            )

    @staticmethod
    def _passphrase(args: Mapping[str, Any]) -> str:
        pw = args.get("passphrase")
        if type(pw) is not str or not 0 < len(pw) <= PASSPHRASE_MAX:
            raise ClickError("invalid.input", "passphrase is text")
        return pw

    def _tier_a(self, entry: dict[str, Any]) -> tuple[str, int]:
        """The switch is tier A only (§0.8.6 item 3a): in tier B the host's flag file belongs to another UID."""
        wid, uid = entry["workspace_id"], int(entry["host_uid"])
        if uid != self.uid:
            raise ClickError(
                "custody.terminal_needed",
                "the host runs as another user here (tier B); switch click-to-sign in the terminal: "
                f"orch host convenience on|off --workspace {entry.get('prefix') or wid}",
            )
        return wid, uid

    def custody_on(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Click-to-sign on, from the dashboard (see the invariant above). The slow part (scrypt, the key store write)
        runs before the flag lock and leaves the selection at step ``written``, where nothing signs with the key; under
        the lock the pending hand-offs and the selection are checked again, then the conversion is finished and the
        flag set last. A hand-off that arrived meanwhile undoes the staged move. The passphrase is never logged,
        stored or returned."""
        from orch.custody import select
        from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound, WrongPassphrase
        from orch.host import convenience
        from orch.host.errors import HostError

        entry = self._entry(args.get("workspace_id"))
        pw = self._passphrase(args)
        wid, uid = self._tier_a(entry)
        st = select.status(self.state, wid)
        if st.get("problem") is not None:
            raise ClickError("custody.unavailable", f"{st['problem']}; see the terminal: orch keys custody status")
        name = entry.get("prefix") or wid
        if st["custody"] == "convenience":
            if self._enabled(entry):
                return self._row(entry)  # already on: nothing changes, nothing is proved
            raise ClickError(
                "custody.terminal_needed",
                "this workspace's device key is already in convenience custody, so the dashboard has no passphrase to "
                f"check; switch it on in the terminal: orch host convenience on --workspace {name}",
            )
        self._custody_budget()
        try:
            convenience.refuse_turn_on(self.state, entry, uid)  # B10: before any passphrase work or key move
        except HostError as exc:
            head = exc.message.split(":", 1)[0]
            raise ClickError(
                exc.code, exc.message, reason=head if head in REASONS else None, detail=exc.detail
            ) from None
        try:
            before = select.read_selection(self.state, wid)
            try:
                staged = select.stage_convenience(self.state, wid, prefix=str(entry.get("prefix") or ""), passphrase=pw)
            except WrongPassphrase:
                self._miss()
                raise ClickError("custody.wrong_passphrase", "the passphrase was refused; nothing changed") from None
            finished = False
            try:
                with convenience.workspace_lock(self.state, wid, uid):
                    select.finish_convenience(self.state, wid, staged)
                    finished = True
                    convenience.set_enabled(self.state, wid, True, uid)  # last: no error after the flag changed
            except HostError:
                # lock.busy before anything was finished: put the key back where it was. After the conversion
                # finished (Opus F3 on #578) there is nothing to undo: convenience custody with the flag off is safe,
                # and the host error itself is what the person needs to see.
                if not finished:
                    select.undo_staged(self.state, wid, staged, before)
                raise
        except HostError as exc:  # lock.busy, custody.unsafe_mode
            raise ClickError(exc.code, exc.message, retryable=exc.code == "lock.busy") from None
        except select.TerminalNeeded as exc:  # V3-D31: the factor was chosen on the dashboard
            raise ClickError("custody.terminal_needed", str(exc)) from None
        except KeyNotFound as exc:
            raise ClickError("custody.no_key", str(exc)) from None
        except (BackendUnavailable, CustodyError) as exc:
            raise ClickError("custody.unavailable", str(exc)) from None
        self.problems.pop(wid, None)
        self._note("custody_on", workspace=wid)
        return self._row(entry)

    def custody_off(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Click-to-sign off, from the dashboard. Without a ``passphrase``: the flag goes off and nothing else. With one
        (V3-D31, narrowing Opus H2 on #539): the flag goes off and the key moves back under that new passphrase, in one
        locked step, and only while the flag is on at that moment (see the comment block above). The text is never
        logged or echoed."""
        from orch.custody import select
        from orch.custody.base import BackendUnavailable, CustodyError, KeyNotFound
        from orch.host.errors import HostError

        entry = self._entry(args.get("workspace_id"))
        wid, uid = self._tier_a(entry)
        pw = args.get("passphrase")
        if "passphrase" in args and (type(pw) is not str or not pw):
            raise ClickError("invalid.input", "passphrase is a non-empty string")
        try:
            select.turn_off_convenience(self.state, wid, uid, move_key=pw is not None, passphrase=pw)
        except HostError as exc:
            raise ClickError(exc.code, exc.message, retryable=exc.code == "lock.busy") from None
        except select.TerminalNeeded as exc:
            if exc.flag_cleared:  # the off happened before the refusal: the log says so (L2-1075-3)
                self._note("custody_off", workspace=wid)
            raise ClickError("custody.terminal_needed", str(exc)) from None
        except KeyNotFound as exc:
            raise ClickError("custody.no_key", str(exc)) from None
        except (BackendUnavailable, CustodyError) as exc:
            raise ClickError("custody.unavailable", str(exc)) from None
        self.problems.pop(wid, None)
        self._note("custody_off", workspace=wid)
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
            "stash": self.stash,
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
