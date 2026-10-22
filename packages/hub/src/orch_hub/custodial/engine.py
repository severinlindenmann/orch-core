"""The custodial engine: ``prepare`` builds the event from the hub's own view, ``sign`` checks the channel proof over
its digest, recomputes the event, and only then signs with the user's ``UK`` (one signer per key kind, protocol.md §3).

Invariants (spec §5, protocol.md §8, L2 review L2-4/L2-5):

* the engine has no entry point that takes event bytes: the event is built here from the :class:`WorkspaceView` (which
  reads the workspace with ``WK_e``, opened for the build only) plus the hub's own snapshot, certificates and channel
  registry;
* ``prepare`` returns the whole signed context ``ctx(E)`` (not just the digest) so the channel can recompute the
  digest and render every authority-bearing field (WYSIWYS, L2-5);
* ``sign`` consumes the prepared request first (single use, short TTL), then checks the proof over the STORED digest
  with the user's CURRENT channel certificate, then rebuilds the event from scratch and refuses unless it is
  identical (TOCTOU: roles, revocations, workspace state or certificates changed in between);
* the signature is made by ``HubKeys.uk_signer(user)`` (a typed ``UkSigner``) only; the finished event is verified
  with the host's own checks (``verify_custodial``) before the audit record is written, and the audit record before
  the event is delivered. Any failure on the way refuses and delivers nothing.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..access import AccessSnapshots
from ..audit import AuditLog
from ..contract import hub as H
from ..contract import phase4 as P4
from ..contract import suite as S
from ..contract.canon import CanonError, b64u, cj, unb64u
from ..contract.hub import Refused
from ..contract.keystore import KeyUseError
from ..contract.labels import EVENT_TYPE
from ..keys import HubKeys, KeysError

TTL_MS = 120_000  # a prepared request lives two minutes: long enough to look at it, short enough to be a prompt
MAX_PER_USER = 4  # outstanding prepared requests per user; over the cap the user's OLDEST is evicted
MAX_PENDING = 4096  # global hard ceiling (memory guard only, far above users x MAX_PER_USER)
_LOG = re.compile(r"workspace|[A-Za-z0-9_-]{1,64}")
_ULID_LOG = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: Fields the engine owns. A view may not supply any of them (an event cannot smuggle its own actor, channel or sig).
RESERVED = frozenset(
    {
        "v",
        "id",
        "seq",
        "at",
        "prev",
        "ws_seq",
        "sig",
        "host_sig",
        "actor",
        *H.CUSTODIAL_FIELDS,
    }
)
REQUIRED_CORE = ("type", "based_on", "roster_v", "hash_v")


class SessionCheck(Protocol):
    """D7's session read interface: True only for a live, authenticated session of THIS user on THIS channel."""

    def valid(self, session: Any, user_id: str, channel_id: str) -> bool: ...


class ChannelRegistry(Protocol):
    """D7's read interface. ``active_channel`` returns the user's signed ``hub.channel_cert`` for that channel, or
    None when the channel is unknown, not this user's, or no longer active (revoked, expired, deregistered)."""

    def active_channel(self, user_id: str, channel_id: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class BuiltEvent:
    """What a :class:`WorkspaceView` returns: the log and the event type's own part. ``core`` holds ``type``, the
    type's payload, ``based_on``, ``roster_v``, ``hash_v`` (and ``gate_gen`` on decisions), and nothing the engine owns."""

    log: str
    core: dict[str, Any]


class WorkspaceView(Protocol):
    """The hub's read of a workspace through ``WK_e`` (the ticket, the gate hash material, the log head). Deterministic:
    the same ``(workspace, user, event_id, action)`` over the same workspace state gives the same ``BuiltEvent``;
    ``sign`` relies on it to detect a change since ``prepare``. ``wk_e`` is valid for this call only."""

    def build(
        self, wk_e: bytearray, epoch: int, workspace_id: str, user_id: str, event_id: str, action: Mapping[str, Any]
    ) -> BuiltEvent: ...


@dataclass(frozen=True)
class Prepared:
    prepared_id: str
    workspace_id: str
    log: str
    ctx: dict[str, Any]  # the signed context ctx(E without channel_proof): what the channel must show and hash
    digest: bytes
    expires_ms: int

    def public(self) -> dict[str, Any]:
        return {
            "prepared_id": self.prepared_id,
            "ctx": self.ctx,
            "digest": self.digest.hex(),
            "expires_ms": self.expires_ms,
        }


@dataclass
class _Pending:
    prepared: Prepared
    user_id: str
    channel_id: str
    action: dict[str, Any]
    event_id: str


def _ulid(now_ms: int) -> str:
    n = (now_ms << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


class CustodialEngine:
    def __init__(
        self,
        *,
        keys: HubKeys,
        snapshots: AccessSnapshots,
        seals: Any,  # WkSeals: ``open_current(ws) -> (epoch, bytearray)``
        view: WorkspaceView,
        channels: ChannelRegistry,
        sessions: SessionCheck,
        audit: AuditLog,
        deliver: Callable[[str, str, dict[str, Any]], None],
        mode_of: Callable[[str], str | None],
        now_ms: Callable[[], int] | None = None,
        ttl_ms: int = TTL_MS,
        dev_mode: bool = True,
    ) -> None:
        self._keys, self._snaps, self._seals, self._view = keys, snapshots, seals, view
        self._channels, self._sessions = channels, sessions
        self._audit, self._deliver, self._mode_of = audit, deliver, mode_of
        self._now = now_ms or (lambda: int(time.time() * 1000))
        self._ttl, self._dev = ttl_ms, dev_mode
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    # --- shared by prepare and sign ----------------------------------------------------------------------------------

    def _build(
        self, workspace_id: str, user_id: str, channel_id: str, event_id: str, action: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Builds E (without ``channel_proof``) from the hub's own state. Returns ``(log, E, snapshot_object)``.
        Every refusal is a :class:`Refused`; nothing unreadable is skipped."""
        if self._mode_of(workspace_id) != "full":  # selective: the hub holds no WK_e, so it builds nothing (D80)
            raise Refused("wrong_mode")
        try:
            card = self._keys.card()
            held = self._keys.verify_card(card)
            revoked_ops = self._keys.revoked_op_certs()
            now = self._now()
            uk_cert = self._keys.uk_cert(user_id)
            uk = H.verify_uk_cert(uk_cert, held, now_ms=now, dev_mode=self._dev, revoked_op_certs=revoked_ops)
        except KeysError:
            raise Refused("other_user") from None
        if uk["user_id"] != user_id:
            raise Refused("other_user")
        ch_cert = self._channels.active_channel(user_id, channel_id)
        if ch_cert is None:
            raise Refused("channel_revoked")
        ch = H.verify_channel_cert(ch_cert, held, now_ms=now, dev_mode=self._dev, revoked_op_certs=revoked_ops)
        if ch["user_id"] != user_id or ch["channel_id"] != channel_id:
            raise Refused("other_user")
        snap = H.verify_snapshot(
            self._snaps.snapshot(workspace_id, "full"),
            held,
            workspace_id=workspace_id,
            dev_mode=self._dev,
            pinned=None,
            revoked_op_certs=revoked_ops,
        )
        if channel_id in snap["revoked_channels"]:
            raise Refused("channel_revoked")
        role = snap["roles"].get(user_id)
        if role not in H.ROLES:
            raise Refused("forbidden_role")
        # §8.5: exact action schema, `(type, log)` in the closed custodial table, role reaches its floor. The only place
        # that decides what a channel may start; the engine has no role table of its own. Raises not_custodial /
        # forbidden_role / malformed in the contract's order.
        P4.check_action(dict(action), role=role)
        epoch, wk = self._seals.open_current(workspace_id)
        try:
            built = self._view.build(wk, epoch, workspace_id, user_id, event_id, action)
        finally:
            for i in range(len(wk)):  # best effort: the key lives in this bytearray only, for this call only
                wk[i] = 0
        if type(built) is not BuiltEvent or type(built.core) is not dict or type(built.log) is not str:
            raise Refused("malformed")
        core = built.core
        if (
            not _LOG.fullmatch(built.log)
            or any(k in core for k in RESERVED)
            or any(k not in core for k in REQUIRED_CORE)
        ):
            raise Refused("malformed")
        typ = core["type"]
        if type(typ) is not str or not EVENT_TYPE.fullmatch(typ):
            raise Refused("malformed")
        # the view may not turn the action into another type or log (defence in depth: re-check what was built)
        want_log = action["log"] if action["type"] != "ticket.created" else None
        if typ != action["type"] or (want_log is not None and built.log != want_log):
            raise Refused("event_changed")
        if want_log is None and not _ULID_LOG.fullmatch(built.log):
            raise Refused("malformed")  # ticket.created: the hub makes the new ticket's uid
        H.check_custodial_role(typ, built.log, role)
        # a decision names the hash the person reviewed (§8.5): the view refuses with event_changed when its current
        # hash moved; the engine also refuses an event whose hash is not the one the person saw.
        # TODO(contract): the event-side field name of the reviewed item hash is not pinned for every type (and the
        # selective-mode item hash is open, §16); until it is, only a `hash` field on the built core is compared.
        if "hash" in action and "hash" in core and core["hash"] != action["hash"]:
            raise Refused("event_changed")
        event = {
            **core,
            "v": 2,
            "id": event_id,
            "actor": {"kind": "person", "id": "u_" + user_id, "via": channel_id},
            "auth": "hub",
            "channel": channel_id,
            "channel_factor": ch["factor"],
            "hub_id": self._keys.hub_id,
            "snapshot_version": snap["version"],
            "channel_cert": ch_cert,
            "uk_cert": uk_cert,
        }
        return built.log, event, snap

    def _digest(self, workspace_id: str, log: str, event: Mapping[str, Any]) -> bytes:
        try:
            return H.event_digest(workspace_id, log, event)
        except Refused, CanonError:
            raise Refused("malformed") from None

    # --- prepare -----------------------------------------------------------------------------------------------------

    def prepare(
        self, session: Any, workspace_id: str, user_id: str, channel_id: str, action: Mapping[str, Any]
    ) -> Prepared:
        """Build the event for ``action`` as ``user_id`` on ``channel_id``. Returns the context the channel must show
        and the digest it signs. ``action`` is the user's request (what to do), never event bytes."""
        for x in (workspace_id, user_id, channel_id):
            if type(x) is not str or len(x) != 32 or x != x.lower() or not _is_hex(x):
                raise Refused("malformed")
        if type(action) is not dict:
            raise Refused("malformed")
        if not self._sessions.valid(session, user_id, channel_id):  # only the user's own session may prepare
            raise Refused("unauthorized")
        now = self._now()
        event_id = _ulid(now)
        log, event, _ = self._build(workspace_id, user_id, channel_id, event_id, action)
        digest = self._digest(workspace_id, log, event)
        ctx = H.signed_context(workspace_id, log, event)
        try:
            P4.check_prepared(ctx, digest.hex())  # what the channel will do with our reply (§8.4 step 3)
        except Refused:
            raise Refused("malformed") from None
        prepared = Prepared(secrets.token_hex(16), workspace_id, log, ctx, digest, now + self._ttl)
        with self._lock:
            self._gc(now)
            mine = [k for k, q in self._pending.items() if q.user_id == user_id]  # insertion order = oldest first
            for k in mine[: max(0, len(mine) - MAX_PER_USER + 1)]:
                del self._pending[k]  # only this user's own oldest ones
            if len(self._pending) >= MAX_PENDING:
                raise Refused("unavailable")
            self._pending[prepared.prepared_id] = _Pending(prepared, user_id, channel_id, dict(action), event_id)
        return prepared

    def _gc(self, now: int) -> None:
        for k in [k for k, p in self._pending.items() if p.prepared.expires_ms <= now]:
            del self._pending[k]

    # --- sign --------------------------------------------------------------------------------------------------------

    def sign(self, prepared_id: str, channel_id: str, proof: str) -> dict[str, Any]:
        """Check the proof, recompute, sign with the user's ``UK``, audit, deliver. Returns the signed event."""
        with self._lock:  # single use: consumed on the first attempt, whatever follows
            p = self._pending.pop(prepared_id, None) if type(prepared_id) is str else None
        if p is None:
            raise Refused("unknown_prepared")
        now = self._now()
        if p.prepared.expires_ms <= now:
            raise Refused("prepared_expired")
        if channel_id != p.channel_id:
            raise Refused("other_user")
        ws, user, pr = p.prepared.workspace_id, p.user_id, p.prepared
        try:
            sig_proof = unb64u(proof, S.SIG_LEN)
        except CanonError, TypeError:
            raise Refused("bad_channel_proof") from None
        # 1. the current channel certificate (revocation, expiry) and the proof over the STORED digest
        log, event, _ = self._build(ws, user, p.channel_id, p.event_id, p.action)  # 2. recompute (TOCTOU)
        try:
            same = log == pr.log and cj(H.signed_context(ws, log, event)) == cj(pr.ctx)
        except CanonError:
            same = False
        if not same or self._digest(ws, log, event) != pr.digest:
            raise Refused("event_changed")
        key_pub = unb64u(event["channel_cert"]["o"]["key_pub"], S.PUB_LEN)
        if not S.verify(key_pub, sig_proof, H.channel_proof_bytes(pr.digest)):
            raise Refused("bad_channel_proof")
        # 3. sign: the typed UK signer, custodial label of this type, over the context including the proof
        e_signed = {**event, "channel_proof": proof}
        try:
            sig = self._keys.uk_signer(user).sign_event(ws, log, e_signed)
        except KeysError, KeyUseError:
            raise Refused("other_user") from None
        signed = {**e_signed, "sig": b64u(sig)}
        # 4. our own output through the host's checks, before anything leaves the hub
        self._self_check(signed, ws, log, now)
        # 5. audit first (fail closed: no record, no delivery), then deliver
        try:
            self._audit.custodial(
                workspace_id=ws,
                log=log,
                verb=event["type"],
                user_id=user,
                channel_id=p.channel_id,
                digest=b64u(pr.digest),
            )
        except Exception:  # noqa: BLE001 (any audit failure refuses; detail stays out of the error)
            raise Refused("unavailable") from None
        self._deliver(ws, log, signed)
        return signed

    def _self_check(self, signed: dict[str, Any], ws: str, log: str, now: int) -> None:
        try:
            held = self._keys.verify_card(self._keys.card())
            snap = H.verify_snapshot(
                self._snaps.snapshot(ws, "full"),
                held,
                workspace_id=ws,
                dev_mode=self._dev,
                pinned=None,
                revoked_op_certs=self._keys.revoked_op_certs(),
            )
            H.verify_custodial(
                signed,
                workspace_id=ws,
                log=log,
                held=held,
                snapshot=snap,
                now_ms=now,
                dev_mode=self._dev,
                revoked_op_certs=self._keys.revoked_op_certs(),
            )
        except KeysError, Refused:
            raise Refused("unavailable") from None


def _is_hex(x: str) -> bool:
    try:
        bytes.fromhex(x)
    except ValueError:
        return False
    return True
