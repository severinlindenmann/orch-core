"""R6c: the pairing state on disk (contract 11A.15.3): ``membership/pairing/`` and ``membership/devices/``.

* ``pairing/<offer_id>.json``: the offer record (11A.15.6), **secret** while it holds ``S`` and ``sas_nonce``
  (dropped at a terminal state plus 10 minutes); rewritten atomically **before** every answer or network write that
  depends on the transition. After 7 days a terminal record shrinks to a tombstone ``{v, offer_id, state, device_id,
  tombstone: true}``.
* ``pairing/<offer_id>.answers``: ``{"v":1, "answers":[{rid, device, digest, at_ms, env}]}``, every answer and
  refusal sent for the offer, written **before** the POST and pruned of entries older than 900 s (the relay's rid
  lifetime) at each write. An identical resend (the same device, ``rid`` and request bytes) replays these bytes;
  nothing is ever re-sealed for it. Keyed by ``(device, rid)`` as R2's rid store (another device naming the same
  ``rid`` is another request: corpus ``pair_resend_then_other_key``).
* ``pairing/outstanding.json``: the open relay cert request of this workspace ``{v, request_id, offer_id,
  expires_ms}``, written before the cert-request POST, cleared when it is signed, rejected or past ``expires_ms + 60
  s``.
* ``pairing/alarms.json``: R6c's pairing alarms and ``needs`` items (``host.status.relay``).
  **JUDGEMENT CALL (R6c):** the table of 11A.15.3 names one ``membership/alarms.json``; ``membership/`` is the key
  store's locked directory (one ``flock`` per directory), so the pair thread keeps its own file here and the status
  merges both. No secret is ever in it.
* ``devices/<device_id>.json``: ``{v, device_id, person_id, cert, label, offer_id, paired_ms, decl_digest, scopes,
  event_id}`` of every relay-reachable device, **write-once** at the allocation (step 9); an equal rewrite is a no-op,
  other bytes are refused.

**Writers** (11A.15.3): the pair thread; the allocation critical section (the host's step 9 hooks, under the store
lock) and the load-time recovery are the only others. All of them go through one :class:`PairStore` per directory
and its lock, so a record read-modify-write is never interleaved. Every file is 0600 under a 0700 directory, written
relative to a locked directory fd (:class:`orch.wsproto.state.StateDir`), never through a symlink.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from orch import canon
from orch.wsproto.state import StateDir, StateError

__all__ = ["ANSWER_LIFE_MS", "PairStore", "PairStoreError", "pairing_present"]

ANSWER_LIFE_MS = 900_000  # the relay's rid lifetime: an answer older than this is never asked for again
_HEX32 = re.compile(r"[0-9a-f]{32}")
_ALARMS = "alarms.json"
_OUTSTANDING = "outstanding.json"
_MAX_ANSWERS = 256  # per offer: 5 refusals a minute plus answers, over 15 minutes, with margin


class PairStoreError(Exception):
    """A pairing file is unreadable, of the wrong shape, or a write-once file holds other bytes. Fail closed: the
    caller treats the workspace's pairing as blocked (never as "no offer")."""


def pairing_present(membership: Path) -> bool:
    """Whether ``membership/pairing`` exists (a pairing ever ran here). Never creates anything: ``membership/``'s
    existence alone makes ``_relay_paired`` true (11A.6)."""
    return (Path(membership) / "pairing").is_dir()


def _check_id(value: object, what: str) -> str:
    if type(value) is not str or not _HEX32.fullmatch(value):
        raise PairStoreError(f"{what} is not 32 lower-case hex")
    return value


class PairStore:
    """``membership/pairing/`` and ``membership/devices/`` of one workspace (one instance per directory: each
    :class:`StateDir` holds an exclusive ``flock``)."""

    def __init__(self, membership: Path) -> None:
        self.root = Path(membership)
        self.lock = threading.RLock()
        self._dir = StateDir(self.root / "pairing")
        try:
            self._devices = StateDir(self.root / "devices")
        except BaseException:
            self._dir.close()
            raise
        self._dir.discard_temps()
        self._devices.discard_temps()

    def close(self) -> None:
        self._devices.close()
        self._dir.close()

    # -- offer records -----------------------------------------------------------------------------------------------

    def offer_ids(self) -> list[str]:
        with self.lock:
            return sorted(n[:-5] for n in self._dir.names() if n.endswith(".json") and _HEX32.fullmatch(n[:-5]))

    def get(self, offer_id: str) -> dict[str, Any] | None:
        _check_id(offer_id, "offer_id")
        with self.lock:
            try:
                rec = self._dir.read_json(f"{offer_id}.json")
            except StateError as e:
                raise PairStoreError(f"offer record unreadable: {type(e).__name__}") from None
        if rec is None:
            return None
        if type(rec) is not dict or rec.get("v") != 1 or rec.get("offer_id") != offer_id or "state" not in rec:
            raise PairStoreError("offer record shape")
        return rec

    def put(self, rec: dict[str, Any]) -> None:
        offer_id = _check_id(rec.get("offer_id"), "offer_id")
        if rec.get("v") != 1 or type(rec.get("state")) is not str:
            raise PairStoreError("offer record shape")
        with self.lock:
            self._dir.write_json(f"{offer_id}.json", rec)

    def remove_answers(self, offer_id: str) -> None:
        _check_id(offer_id, "offer_id")
        with self.lock:
            self._dir.remove(f"{offer_id}.answers")

    # -- the per-rid answers (Opus L1) ---------------------------------------------------------------------------------

    def answers(self, offer_id: str) -> list[dict[str, Any]]:
        _check_id(offer_id, "offer_id")
        with self.lock:
            try:
                obj = self._dir.read_json(f"{offer_id}.answers")
            except StateError as e:
                raise PairStoreError(f"answers unreadable: {type(e).__name__}") from None
        if obj is None:
            return []
        if type(obj) is not dict or obj.get("v") != 1 or type(obj.get("answers")) is not list:
            raise PairStoreError("answers shape")
        return list(obj["answers"])

    def answer_for(self, offer_id: str, device: str, rid: str) -> dict[str, Any] | None:
        return next((a for a in self.answers(offer_id) if a.get("rid") == rid and a.get("device") == device), None)

    def add_answer(self, offer_id: str, device: str, rid: str, digest: str, at_ms: int, env_b64: str) -> None:
        """Durable before the POST. A second answer for one ``(device, rid)`` is refused (the stored one is
        replayed)."""
        _check_id(rid, "rid")
        _check_id(device, "device")
        with self.lock:
            kept = [a for a in self.answers(offer_id) if at_ms - int(a.get("at_ms", 0)) < ANSWER_LIFE_MS]
            if any(a.get("rid") == rid and a.get("device") == device for a in kept):
                raise PairStoreError("an answer for this rid is stored already")
            kept.append({"rid": rid, "device": device, "digest": digest, "at_ms": at_ms, "env": env_b64,
                         "posted": False})  # fmt: skip
            self._dir.write_json(f"{offer_id}.answers", {"v": 1, "answers": kept[-_MAX_ANSWERS:]})

    def mark_answer_posted(self, offer_id: str, device: str, rid: str) -> None:
        """The relay took this answer (or refused it for good): the tick no longer re-posts it."""
        with self.lock:
            kept = self.answers(offer_id)
            for a in kept:
                if a.get("rid") == rid and a.get("device") == device and a.get("posted") is False:
                    a["posted"] = True
                    self._dir.write_json(f"{offer_id}.answers", {"v": 1, "answers": kept})
                    return

    # -- the outstanding relay cert request (Codex 2, Opus L2) ---------------------------------------------------------

    def outstanding(self) -> dict[str, Any] | None:
        with self.lock:
            try:
                obj = self._dir.read_json(_OUTSTANDING)
            except StateError as e:
                raise PairStoreError(f"outstanding unreadable: {type(e).__name__}") from None
        if obj is None:
            return None
        if type(obj) is not dict or set(obj) != {"v", "request_id", "offer_id", "expires_ms"}:
            raise PairStoreError("outstanding shape")
        return obj

    def set_outstanding(self, request_id: str, offer_id: str, expires_ms: int) -> None:
        with self.lock:
            self._dir.write_json(
                _OUTSTANDING,
                {"v": 1, "request_id": _check_id(request_id, "request_id"),
                 "offer_id": _check_id(offer_id, "offer_id"), "expires_ms": int(expires_ms)},
            )  # fmt: skip

    def clear_outstanding_unchecked(self) -> None:
        """Remove ``outstanding.json`` whatever it holds (only the load-time quarantine of an unreadable one)."""
        with self.lock:
            self._dir.remove(_OUTSTANDING)

    def clear_outstanding(self, request_id: str | None = None) -> None:
        """Clear it (only if it names ``request_id`` when one is given)."""
        with self.lock:
            cur = self.outstanding()
            if cur is not None and (request_id is None or cur["request_id"] == request_id):
                self._dir.remove(_OUTSTANDING)

    # -- alarms and needs items ----------------------------------------------------------------------------------------

    def alarms(self) -> dict[str, Any]:
        with self.lock:
            try:
                obj = self._dir.read_json(_ALARMS)
            except StateError:
                return {"v": 1, "alarms": {"pairing_state_unreadable": {"count": 1, "at_ms": 0, "detail": "alarms"}},
                        "needs": []}  # fmt: skip
        if type(obj) is not dict or type(obj.get("alarms")) is not dict or type(obj.get("needs")) is not list:
            return {"v": 1, "alarms": {}, "needs": []}
        return obj

    def write_alarms(self, obj: dict[str, Any]) -> None:
        with self.lock:
            self._dir.write_json(_ALARMS, obj)

    # -- relay-reachable devices -------------------------------------------------------------------------------------

    def device(self, device_id: str) -> dict[str, Any] | None:
        _check_id(device_id, "device_id")
        with self.lock:
            try:
                return self._devices.read_json(f"{device_id}.json")
            except StateError as e:
                raise PairStoreError(f"device record unreadable: {type(e).__name__}") from None

    def device_ids(self) -> list[str]:
        with self.lock:
            return sorted(n[:-5] for n in self._devices.names() if n.endswith(".json") and _HEX32.fullmatch(n[:-5]))

    def write_device(self, rec: dict[str, Any]) -> None:
        """Write-once (claimed with ``os.link``): an equal record is a no-op, other bytes are refused."""
        device_id = _check_id(rec.get("device_id"), "device_id")
        data = canon.dumps(rec)
        with self.lock:
            if self._devices.claim(f"{device_id}.json", data):
                return
            held = self.device(device_id)
            if held is None or canon.dumps(held) != data:
                raise PairStoreError("another record of this device is stored")
