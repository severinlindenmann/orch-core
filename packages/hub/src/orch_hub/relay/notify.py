"""``PushNotifier``: push to the live channels of a user, and clean up at the relay when a channel is revoked.

Hook points (kept small, both optional callables on the services, never able to break them):
``CustodialEngine(notify=...)`` is called as ``notify(user_id, "prepare", prepared_id)`` after a prepare is stored, and
``Channels(on_revoke=...)`` as ``on_revoke(channel_id)`` after a revocation is final. ``notify_user(user_id, "needs_you",
ref)`` is the call for a needs-you change (no caller yet). The notifier only ever pushes to channels the hub itself lists
as active (unrevoked, unexpired, current cert, §6.2) and of kind ``phone`` (only phones have an APNs target); the relay is
never asked who is live.

A failed revocation cleanup is kept in ``pending_cleanup`` and repeated by :meth:`flush` (called at the start of every
``notify_user`` as well); it is in memory, so a restart forgets it (follow-up: persist). That is safe: nothing is ever
sent to a revoked channel, and what the relay still holds for it is a sealed token it cannot use.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..contract.canon import CanonError, unb64u
from .client import RelayClient, RelayError

log = logging.getLogger(__name__)


class PushNotifier:
    def __init__(self, client: RelayClient, channels: Any) -> None:
        self._client, self._channels = client, channels
        self._lock = threading.Lock()
        self.pending_cleanup: set[str] = set()

    # --- the phone gave its APNs token (over its authenticated channel session; the caller has checked that) -------

    def register_target(self, user_id: str, channel_id: str, env: str, topic: str, token: bytes) -> None:
        """Register or replace the push target of one of the user's own live phone channels."""
        if self._cert_pub(user_id, channel_id) is None:
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
        with ``sent``, ``no_target`` (the relay holds no push target) or the relay's error code."""
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
                out[cid] = e.code
                log.warning("push to %s failed: %s", cid[:8], e.code)
            except Exception:  # noqa: BLE001
                out[cid] = "error"
                log.warning("push to %s failed", cid[:8])
        return out

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

    def flush(self) -> None:
        with self._lock:
            todo = sorted(self.pending_cleanup)
        for cid in todo:
            self.channel_revoked(cid)
