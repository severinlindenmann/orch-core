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

import logging
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

_log = logging.getLogger(__name__)

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
        "item_hash",  # selective mode only, and only from the shared item (§8.6)
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


def verified_channel(
    channels: ChannelRegistry,
    user_id: str,
    channel_id: str,
    *,
    held: Mapping[str, Any],
    now_ms: int,
    dev_mode: bool,
    revoked_op_certs: frozenset[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The user's CURRENT channel certificate (registered, not revoked, not expired) checked against the held card:
    ``(signed cert, verified object)``. Shared by the custodial ``prepare`` and the owner's access-change approval, so both
    read a channel the same way."""
    ch_cert = channels.active_channel(user_id, channel_id)
    if ch_cert is None:
        raise Refused("channel_revoked")
    ch = H.verify_channel_cert(ch_cert, held, now_ms=now_ms, dev_mode=dev_mode, revoked_op_certs=revoked_op_certs)
    if ch["user_id"] != user_id or ch["channel_id"] != channel_id:
        raise Refused("other_user")
    return ch_cert, ch


def check_channel_proof(
    *,
    keys: HubKeys,
    channels: ChannelRegistry,
    user_id: str,
    channel_id: str,
    digest: bytes,
    proof: str,
    now_ms: int,
    min_factor: str = "password+totp",
) -> bytes:
    """A channel proof over ``digest`` exactly as ``CustodialEngine.sign`` checks it: the proof is a canonical 64-byte
    signature (else ``bad_channel_proof``), the channel certificate is the user's current one, its factor reaches
    ``min_factor`` (``factor_too_weak``; the console's browser keys are ``password+totp``) and the signature verifies
    over ``"orch/v2/hub-channel-proof|" || digest`` under the certificate's key. Returns the decoded proof."""
    try:
        sig = unb64u(proof, S.SIG_LEN)
    except CanonError, TypeError:
        raise Refused("bad_channel_proof") from None
    try:
        card = keys.card()
        held = keys.verify_card(card)
        revoked_ops = keys.revoked_op_certs()
        dev = bool(card["o"]["dev"])
    except KeysError:
        raise Refused("unavailable") from None
    _, ch = verified_channel(
        channels, user_id, channel_id, held=held, now_ms=now_ms, dev_mode=dev, revoked_op_certs=revoked_ops
    )
    if H.FACTOR_RANK.get(ch["factor"], -1) < H.FACTOR_RANK[min_factor]:
        raise Refused("factor_too_weak")
    if not S.verify(unb64u(ch["key_pub"], S.PUB_LEN), sig, H.channel_proof_bytes(digest)):
        raise Refused("bad_channel_proof")
    return sig


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
        dev_mode: bool,
        notify: Callable[[str, str, str | None], None] | None = None,
        shares: Any = None,
    ) -> None:
        #: selective mode's item source: ``shared_item(user_id, workspace_id, item_id)`` (:class:`ShareService`)
        self._shares = shares
        #: ``notify(user_id, "prepare", prepared_id)`` after a prepare is stored (push to the user's phones, D10)
        self._notify = notify
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
        mode = self._mode_of(workspace_id)
        if mode not in ("full", "selective"):
            raise Refused("wrong_mode")
        if mode == "selective" and self._shares is None:
            raise Refused("wrong_mode")  # no item source wired: a selective hub builds nothing (D80)
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
        ch_cert, ch = verified_channel(
            self._channels, user_id, channel_id, held=held, now_ms=now, dev_mode=self._dev, revoked_op_certs=revoked_ops
        )
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
        if mode == "selective":
            log, core = self._core_selective(workspace_id, user_id, action)
            role = "edit"  # the decisions' floor; no hub role exists here and the host checks the real member role
        else:
            role = snap["roles"].get(user_id)
            if role not in H.ROLES:
                raise Refused("forbidden_role")
            log, core = self._core_full(workspace_id, user_id, event_id, action, role)
        typ = core["type"]
        if type(typ) is not str or not EVENT_TYPE.fullmatch(typ):
            raise Refused("malformed")
        # the builder may not turn the action into another type or log, and a decision carries the hash the person
        # reviewed (contract check_built, always runs: missing hash = malformed, another = event_changed)
        P4.check_built(self._plain_action(action), log, core)
        if action["type"] == "ticket.created" and not _ULID_LOG.fullmatch(log):
            raise Refused("malformed")  # ticket.created: the hub makes the new ticket's uid
        H.check_custodial_role(typ, log, role)
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
        return log, event, snap

    @staticmethod
    def _plain_action(action: Mapping[str, Any]) -> dict[str, Any]:
        """The action without ``item`` (§8.6): what ``check_built`` compares the built event with."""
        return {k: v for k, v in action.items() if k != "item"}

    def _core_full(
        self, workspace_id: str, user_id: str, event_id: str, action: Mapping[str, Any], role: str
    ) -> tuple[str, dict[str, Any]]:
        """Full mode: the event core from the hub's ``WK_e`` view (§8.4)."""
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
        return built.log, core

    def _core_selective(self, workspace_id: str, user_id: str, action: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Selective mode (§8.6): the event core from ONE shared item. Only the owner alias reads items (``not_granted``
        otherwise); the contract's ``build_selective`` derives every value (nothing in this engine does)."""
        item_id = action.get("item") if type(action) is dict else None
        if type(item_id) is not str or len(item_id) != 32 or item_id != item_id.lower() or not _is_hex(item_id):
            raise Refused("malformed")
        try:
            got = self._shares.shared_item(user_id, workspace_id, item_id)
        except Refused:
            raise
        except Exception:  # noqa: BLE001 (an unreadable item store is not a view: fail closed, detail stays out)
            raise Refused("unavailable") from None
        log, core = P4.build_selective(dict(action), got["signed"])
        if core["item_hash"] != got["hash"]:
            raise Refused("event_changed")  # the held hash and the signed item disagree: never build on it
        return log, core

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
        if self._notify is not None:
            try:
                self._notify(user_id, "prepare", prepared.prepared_id)
            except Exception:  # noqa: BLE001 - a push hint must never fail or slow the signing flow
                _log.warning("prepare notify hook failed")
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
        self._self_check(signed, ws, log, now, p.action.get("item"))
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
        try:
            self._deliver(ws, log, signed)
        except Exception:  # noqa: BLE001 (not queued = not sent: the person can sign again; detail stays out)
            _log.exception("could not hand the signed event over for delivery")
            raise Refused("unavailable") from None
        return signed

    def _self_check(self, signed: dict[str, Any], ws: str, log: str, now: int, item_id: Any = None) -> None:
        selective = "item_hash" in signed  # the one marker of selective mode in an event (§8.1)
        try:
            held = self._keys.verify_card(self._keys.card())
            snap = H.verify_snapshot(
                self._snaps.snapshot(ws, "selective" if selective else "full"),
                held,
                workspace_id=ws,
                dev_mode=self._dev,
                pinned=None,
                revoked_op_certs=self._keys.revoked_op_certs(),
            )
            extra = self._selective_admission(signed, ws, log, item_id) if selective else {}
            self._verify_signed(signed, ws, log, held, snap, now, **extra)
        except KeysError, Refused:
            raise Refused("unavailable") from None

    def _selective_admission(self, signed: Mapping[str, Any], ws: str, log: str, item_id: Any) -> dict[str, Any]:
        """What the host will hold when it admits ``signed`` (core §15.7), as far as the hub can see it: the owner alias
        is the granted user and an owner, the live item is the one the hub shows. The chain head of the item stands in
        for the host's log (the hub cannot read it): this proves the event's own consistency with the item (every value
        of §8.3 step 7), not the host's log, which only the host can check."""
        user = signed["actor"]["id"][2:]
        got = self._shares.shared_item(user, ws, item_id)
        o = got["item"]
        chain = o["chain"]
        heads = [chain["head"]] * chain["seq"]
        return {
            "mode": "selective",
            "granted_users": frozenset({user}),
            "member_roles": {user: "owner"},
            "live_items": {got["hash"]: {"chain": chain, "f1": o["f1"]}},
            "log_heads": {log: heads},
        }

    def _verify_signed(
        self,
        signed: Mapping[str, Any],
        ws: str,
        log: str,
        held: Mapping[str, Any],
        snap: Mapping[str, Any],
        now: int,
        **admission: Any,
    ) -> dict[str, Any]:
        """The host's checks of a finished event (``verify_custodial``) with this engine's dev mode and op-cert
        revocations. Selective mode passes ``mode``, ``granted_users``, ``live_items``, ... (the vectors do the same)."""
        return H.verify_custodial(
            signed,
            workspace_id=ws,
            log=log,
            held=held,
            snapshot=snap,
            now_ms=now,
            dev_mode=self._dev,
            revoked_op_certs=self._keys.revoked_op_certs(),
            **admission,
        )


def _is_hex(x: str) -> bool:
    try:
        bytes.fromhex(x)
    except ValueError:
        return False
    return True
