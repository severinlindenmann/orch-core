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


class PushNotifier:
    def __init__(self, client: RelayClient, channels: Any) -> None:
        self._client, self._channels = client, channels
        self._lock = threading.Lock()
        self.pending_cleanup: set[str] = set()
        self._q: queue.Queue[tuple[str, str, str | None]] = queue.Queue(QUEUE_MAX)
        self._worker: threading.Thread | None = None

    # --- the phone gave its APNs token (over its authenticated channel session; the caller has checked that) -------

    def register_target(self, user_id: str, channel_id: str, env: str, topic: str, token: bytes) -> None:
        """Register or replace the push target of one of the user's own live phone channels."""
        if self._cert_pub(user_id, channel_id) is None or not any(
            c["channel_id"] == channel_id and c["kind"] == "phone" for c in self._channels.channels(user_id)
        ):
            raise RelayError("unknown_channel")
        self._client.put_push_target(channel_id, env, topic, token)

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
        out = []
        for ch in self._channels.channels(user_id):
            if ch["status"] == "active" and ch["kind"] == "phone":
                pub = self._cert_pub(user_id, ch["channel_id"])
                if pub is not None:
                    out.append((ch["channel_id"], pub))
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
        for cid, pub in targets:
            try:
                out[cid] = "sent" if self._client.send_push(cid, pub, kind, ref) else "no_target"
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
        be reached; :meth:`flush` repeats it. Idempotent at the relay."""
        try:
            self._client.delete_push_target(channel_id)
            self._client.mailbox_delete(channel_id)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self.pending_cleanup.add(channel_id)
            log.warning("relay cleanup of %s pending: %s", channel_id[:8], getattr(e, "code", "error"))
            return False
        with self._lock:
            self.pending_cleanup.discard(channel_id)
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
