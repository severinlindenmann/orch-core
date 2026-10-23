"""``PushNotifier``: push to the live channels of a user, and clean up at the relay when a channel is revoked.

Hook points (kept small, both optional callables on the services, never able to break them):
``CustodialEngine(notify=...)`` is called as ``notify(user_id, "prepare", prepared_id)`` after a prepare is stored, and
``Channels(on_revoke=...)`` as ``on_revoke(channel_id)`` after a revocation is final. ``notify_user(user_id, "needs_you",
ref)`` is the call for a needs-you change (no caller yet). The notifier only ever pushes to channels the hub itself lists
as active (unrevoked, unexpired, current cert, §6.2) and of kind ``phone`` (only phones have an APNs target); the relay is
never asked who is live.

A failed revocation cleanup is kept in ``pending_cleanup`` and repeated by :meth:`flush` (called at the start of every
``notify_user`` as well). The set is in memory, so :meth:`restart_pass` rebuilds it at start from the channel store's
revoked channels (the durable truth) and re-sends both DELETEs (idempotent at the relay).

``notify_async`` is the hook for ``CustodialEngine(notify=...)`` in a running hub: it only enqueues (bounded; a full
queue drops the hint, which is only a hint) and a daemon worker does the relay calls, so a slow or dead relay never
stalls a prepare. ``notify_user`` itself stays synchronous (tests, the worker).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from ..contract import suite as S
from ..contract.canon import CanonError, unb64u
from .client import RelayClient, RelayError

log = logging.getLogger(__name__)

#: relay codes that may be logged verbatim; anything else (a relay is not trusted for log text) is ``bad_response``
KNOWN_CODES = frozenset(
    {
        "malformed", "too_large", "not_found", "message_conflict", "quota_exceeded", "overloaded", "not_allowed",
        "bad_hub_signature", "unknown_hub", "stale_op_cert", "other_hub", "replay", "stale_timestamp", "unavailable",
        "bad_response", "dev_mismatch", "no_relay", "unknown_channel", "internal",
    }
)  # fmt: skip
QUEUE_MAX = 256


def _safe_code(code: object) -> str:
    return code if isinstance(code, str) and code in KNOWN_CODES else "bad_response"


class MemoryPushKeys(dict):  # type: ignore[type-arg]
    """The in-memory push-key store (tests); a running hub passes ``channels.pushkeys.PushKeyStore`` instead."""

    def __init__(self) -> None:
        super().__init__()
        self._issued: dict[str, int] = {}

    def put(self, channel_id: str, push_pub: bytes, issued_ms: int) -> bool:
        held = self._issued.get(channel_id)
        if held is not None and issued_ms <= held:
            return False
        self[channel_id], self._issued[channel_id] = push_pub, issued_ms
        return True

    def held_issued_ms(self, channel_id: str) -> int | None:
        return self._issued.get(channel_id)

    def pop(self, channel_id: str, *default: Any) -> Any:
        self._issued.pop(channel_id, None)
        return super().pop(channel_id, *default)


class PushNotifier:
    def __init__(self, client: RelayClient, channels: Any, push_keys: Any = None) -> None:
        """``push_keys``: channel id -> the push key the phone registered (§6.4 ``hub.channel_push``, verified by the
        web API). In memory by default; a running hub passes a persistent store (H1). No key, no hint (§10.8.2)."""
        self._client, self._channels = client, channels
        self._push_keys: Any = push_keys if push_keys is not None else MemoryPushKeys()
        self._lock = threading.Lock()
        self.pending_cleanup: set[str] = set()
        self._q: queue.Queue[tuple[str, str, str | None]] = queue.Queue(QUEUE_MAX)
        self._worker: threading.Thread | None = None

    # --- the phone gave its APNs token (over its authenticated channel session; the caller has checked that) -------

    def register_target(
        self,
        user_id: str,
        channel_id: str,
        env: str,
        topic: str,
        token: bytes,
        push_pub: bytes | None = None,
        push_issued_ms: int | None = None,
    ) -> None:
        """Register or replace the push target of one of the user's own live phone channels. ``push_pub`` is the push
        key of a ``hub.channel_push`` the caller verified (§6.4); ``None`` keeps the held one. It is never the
        channel's signing key (``malformed``)."""
        cert_pub = self._cert_pub(user_id, channel_id)
        if cert_pub is None or not any(
            c["channel_id"] == channel_id and c["kind"] == "phone" for c in self._channels.channels(user_id)
        ):
            raise RelayError("unknown_channel")
        if push_pub is not None and (not S.valid_public_key(push_pub) or push_pub == cert_pub):
            raise RelayError("malformed")
        self._client.put_push_target(channel_id, env, topic, token)
        # the key is stored only after the relay accepted the target, so a failed relay call leaves the held key and its
        # issued_ms untouched and the phone can retry the same registration (L2H1-5)
        if push_pub is not None and push_issued_ms is None:
            self._push_keys[channel_id] = push_pub  # direct caller (tests): no registration object, no replay rule
        elif push_pub is not None and not self._push_keys.put(channel_id, push_pub, push_issued_ms):
            raise RelayError("replay")  # an older registration won the race

    def held_push_issued_ms(self, channel_id: str) -> int | None:
        """``issued_ms`` of the ``hub.channel_push`` behind the held push key (None = no key): what a new registration
        must beat (``verify_channel_push``, ``replay``)."""
        with self._lock:
            return self._push_keys.held_issued_ms(channel_id)

    # --- push -----------------------------------------------------------------------------------------------------

    def _cert_pub(self, user_id: str, channel_id: str) -> bytes | None:
        cert = self._channels.active_channel(user_id, channel_id)
        if cert is None:
            return None
        try:
            return unb64u(cert["o"]["key_pub"], 65)
        except CanonError, KeyError, TypeError:
            return None

    def live_phone_channels(self, user_id: str) -> list[tuple[str, bytes]]:
        """``(channel_id, push_pub)`` of every live phone channel of the user that registered a push key: a channel
        without one gets no hint at all (§10.8.2), and the signing key is never a recipient."""
        out = []
        for ch in self._channels.channels(user_id):
            if ch["status"] == "active" and ch["kind"] == "phone":
                cid = ch["channel_id"]
                cert_pub = self._cert_pub(user_id, cid)
                with self._lock:
                    push = self._push_keys.get(cid)
                if cert_pub is not None and push is not None and push != cert_pub:
                    out.append((cid, push))
        return out

    def notify_user(self, user_id: str, kind: str, ref: str | None = None) -> dict[str, str]:
        """Send one hint to every live phone channel of ``user_id``. Never raises; returns ``{channel_id: result}``
        with ``sent``, ``no_target`` (the relay holds no push target) or ``failed`` (the relay's code is logged)."""
        self.flush()
        out: dict[str, str] = {}
        try:
            targets = self.live_phone_channels(user_id)
        except Exception:  # noqa: BLE001 - a hook must never break the caller; the failure is logged by type only
            log.warning("push: channel list failed")
            return out
        for cid, push_pub in targets:
            try:
                out[cid] = "sent" if self._client.send_push(cid, push_pub, kind, ref) else "no_target"
            except RelayError as e:
                out[cid] = "failed"
                log.warning("push to %s failed: %s", cid[:8], _safe_code(e.code))
            except Exception:  # noqa: BLE001
                out[cid] = "failed"
                log.warning("push to %s failed", cid[:8])
        return out

    def notify_async(self, user_id: str, kind: str, ref: str | None = None) -> None:
        """Queue a hint and return at once (the engine hook). Never raises, never blocks."""
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, daemon=True)
                self._worker.start()
        try:
            self._q.put_nowait((user_id, kind, ref))
        except queue.Full:
            log.warning("push queue full: hint dropped")

    def _work(self) -> None:
        while True:
            user_id, kind, ref = self._q.get()
            try:
                self.notify_user(user_id, kind, ref)
            except Exception:  # noqa: BLE001
                log.warning("push worker failed")
            finally:
                self._q.task_done()

    def drain(self) -> None:
        """Wait until every queued hint has been handled (tests, orderly shutdown)."""
        self._q.join()

    # --- revocation -----------------------------------------------------------------------------------------------

    def channel_revoked(self, channel_id: str) -> bool:
        """Delete the channel's push target and mailbox at the relay. False (and remembered) when the relay could not
        be reached; :meth:`flush` repeats it. Idempotent at the relay. The purge goes first: it is the relay's
        revocation record and is answered with or without a push configuration; a relay without one answers the push
        ``DELETE`` ``404 not_found``, which holds no target and counts as done (§10.8.1, E5R-1)."""
        try:
            self._client.mailbox_delete(channel_id)
            try:
                self._client.delete_push_target(channel_id)
            except RelayError as e:
                if (e.status, e.code) != (404, "not_found"):
                    raise
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.pending_cleanup.add(channel_id)
            log.warning("relay cleanup of %s pending: %s", channel_id[:8], getattr(e, "code", "error"))
            return False
        with self._lock:
            self.pending_cleanup.discard(channel_id)
            self._push_keys.pop(channel_id, None)
        return True

    def channel_revoked_async(self, channel_id: str) -> None:
        """The hook for ``Channels(on_revoke=...)`` in a running hub: the cleanup (with its retries and sleeps) runs on a
        daemon thread, so a revocation never waits for the relay."""
        threading.Thread(target=self.channel_revoked, args=(channel_id,), daemon=True).start()

    def restart_pass(self) -> None:
        """At start: every channel the store lists as revoked gets both DELETEs again (idempotent), so a restart cannot
        leave a revoked channel's target or mailbox at the relay. Failures stay in ``pending_cleanup``."""
        with self._lock:
            self.pending_cleanup |= {c["channel_id"] for c in self._channels.channels() if c["status"] == "revoked"}
        self.flush()

    def restart_pass_async(self) -> None:
        threading.Thread(target=self.restart_pass, daemon=True).start()

    def flush(self) -> None:
        with self._lock:
            todo = sorted(self.pending_cleanup)
        for cid in todo:
            self.channel_revoked(cid)
