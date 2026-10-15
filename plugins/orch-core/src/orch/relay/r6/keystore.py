"""R6a: membership key custody and the seal lock (contract 11A.15.1 to 11A.15.3, 11A.15.5 "the first card").

State, under ``<state>/hosts/<ws>/membership/`` (0700, host UID; every file 0600, written and read relative to a
locked directory fd through :class:`orch.relay.state.StateDir`, never through a symlink):

* ``head.json``: the one atomic record of 11A.15.3 (``lhw``, ``ehw``, ``committed``, ``exposed``, ``burned``, the
  gate, the card numbers; ``holders``, ``excluded``, ``h8`` and ``unmatched_rotation_ms`` are R6b's and carried
  verbatim);
* ``keys/wk-<e>.json`` (``WK_e``), ``keys/wxk-<v>.json`` (the WXK private scalar), ``keys/ck-<s>.json`` (``CK_s``):
  **secret**, each claimed write-once (``os.link`` of a fsynced temp file) **before** the ``head.json`` write or the
  signature that names it;
* ``cards/card-<s>.json``: the exact signed card, claimed before its first ``PUT card``;
* ``sign-ledger.json``: the R6 signer's last-signed ledger (cards, member lists, ``wk_commit`` per epoch, grants).

**Never reuse an epoch.** At load ``ehw := max(head.ehw, every wk-* file, every wk_commit the signer ledger holds)``
and ``lhw := max(head.lhw, every member list in the ledger)``; :meth:`MembershipKeys.new_epoch_key` refuses an epoch
at or below that floor, and the claim refuses an existing file. **The orphan rule:** a key file above the committed
epoch that ``head.json`` never exposed is burned at load (durably, before anything is served): never activated,
never generated again. **H7:** ``head.json`` missing or unreadable while key, card or ledger state exists, the
committed epoch's key missing, or a key that does not match its recorded ``wk_commit`` makes the store *unsafe*: it
serves no key, the worker must not start (:meth:`MembershipKeys.check_start`, alarm ``membership_state_unsafe``), and
only :meth:`MembershipKeys.recover_h7` (R6b, after a verified ``GET members``) clears it, with both high-water marks
raised by 1024 above everything known.

**The seal lock** (11A.15.2) is this object's ``RLock``. It guards the in-memory seal state only (committed epoch,
gate, burned epochs, pending epoch); nothing under it does I/O or takes another lock. Lock order: the store lock, then
``head`` lock (R6's single writer), then the seal lock, then R2's RidStore lock. :class:`R6Gate` is the store's hook:
it takes only the seal lock and queues the durable write (:meth:`MembershipKeys.flush_gate`, on R6's thread).

**No raw key leaves except through the R2 interface** (``wk``/``refusal_key``, called only by
``orch.relay.keys.BridgeKeys``) and ``CK`` inside this package for the card. Nothing here logs, formats or raises with
key bytes; ``repr`` names only the workspace and the committed epoch. Convenience custody does not exist here: the
keys live only in these 0600 files under the host UID, there is no backend parameter, and the package never imports
``orch.custody`` (the import-ban test).
"""

from __future__ import annotations

import contextlib
import hmac
import json
import re
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.identity import pairing, wscard
from orch.identity.errors import Refused

from ..keys import SealState
from ..state import FileSignLedger, StateDir, StateError

__all__ = [
    "CLAMP",
    "FIRST_EPOCH_MAX",
    "GATE_REASONS",
    "H7_MARGIN",
    "MAX_EPOCH",
    "CustodyRefused",
    "MembershipKeys",
    "MembershipUnsafe",
    "R6Gate",
]

MAX_EPOCH = 2**32 - 1
FIRST_EPOCH_MAX = 2**16  # proto §7.2 check 4: the first list's epoch
CLAMP = 2**16  # 11A.15.4: a rotation target is at most committed + 2^16
H7_MARGIN = 1024  # §11 H7: both high-water marks raised by 1024 above everything known
MAX_SAFE_INT = 2**53 - 1
HEAD = "head.json"
GATE_REASONS = frozenset(
    {"device.removed", "device.revoked", "held_evidence", "relay_revocation", "revoked_refusal", "rotation_due",
     "h7", "rotate"}
)  # fmt: skip

_WK = re.compile(r"wk-([1-9][0-9]{0,9})\.json")
_WXK = re.compile(r"wxk-([1-9][0-9]{0,15})\.json")
_CK = re.compile(r"ck-([1-9][0-9]{0,9})\.json")
_CARD = re.compile(r"card-([1-9][0-9]{0,9})\.json")
_DEC = re.compile(r"[1-9][0-9]{0,9}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEAD_FIELDS = frozenset(
    {"v", "lhw", "ehw", "committed", "exposed", "burned", "holders", "excluded", "gate", "h8",
     "unmatched_rotation_ms", "card"}
)  # fmt: skip
_COMMITTED = frozenset({"list_seq", "epoch", "list_hash", "wk_commit", "list", "at_ms"})
_EXPOSED = frozenset({"list_seq", "epoch", "new_epoch", "body", "digest", "adds", "first_send_ms", "attempts"})
_GATE = frozenset({"up", "reasons", "since_ms", "target"})
_CARD_NUMS = frozenset({"card_seq", "wxk_version", "ck_seq", "pending_card_seq", "wxk_retired"})


class MembershipUnsafe(Exception):  # noqa: N818 - a verdict on the state directory
    """The membership state cannot be trusted (H7 host-state loss, 11A.15.4): ``code`` names the cause, never any
    key or file content."""

    def __init__(self, code: str) -> None:
        super().__init__(f"membership state unsafe: {code}")
        self.code = code


class CustodyRefused(ValueError):
    """A custody request outside the contract (an epoch at or below the high-water mark, above the clamp, a second
    key for an epoch, ...). ``what`` names the rule; never any key bytes."""

    def __init__(self, what: str) -> None:
        super().__init__(f"membership custody refused: {what}")
        self.what = what


def _fresh_head() -> dict[str, Any]:
    return {
        "v": 1, "lhw": 0, "ehw": 0, "committed": None, "exposed": None,
        "burned": {"epochs": {}, "list_seqs": []}, "holders": {}, "excluded": {}, "gate": None,
        "h8": {"count": 0, "alarm": False}, "unmatched_rotation_ms": None,
        "card": {"card_seq": 0, "wxk_version": 0, "ck_seq": 0, "pending_card_seq": None, "wxk_retired": []},
    }  # fmt: skip


def _int(v: object, low: int, high: int) -> bool:
    return type(v) is int and low <= v <= high


def _check_head(obj: object) -> dict[str, Any]:
    """The shape of ``head.json`` as far as R6a reads it (exact top-level and sub-record field sets, integer ranges,
    the high-water marks at or above every recorded number); anything else is ``head_shape`` (H7)."""
    bad = MembershipUnsafe("head_shape")
    if type(obj) is not dict or set(obj) != _HEAD_FIELDS or obj["v"] != 1:
        raise bad
    lhw, ehw = obj["lhw"], obj["ehw"]
    if not _int(lhw, 0, MAX_SAFE_INT) or not _int(ehw, 0, MAX_EPOCH):
        raise bad
    c = obj["committed"]
    if c is not None:
        if type(c) is not dict or set(c) != _COMMITTED:
            raise bad
        if not _int(c["epoch"], 1, ehw) or not _int(c["list_seq"], 1, lhw) or not _int(c["at_ms"], 0, MAX_SAFE_INT):
            raise bad
        if type(c["wk_commit"]) is not str or type(c["list_hash"]) is not str:
            raise bad
    x = obj["exposed"]
    if x is not None:
        if type(x) is not dict or set(x) != _EXPOSED:
            raise bad
        if not _int(x["list_seq"], 1, lhw) or not _int(x["epoch"], 1, ehw):
            raise bad
        if x["new_epoch"] is not None and (not _int(x["new_epoch"], 1, ehw) or x["new_epoch"] != x["epoch"]):
            raise bad
        if c is not None and x["list_seq"] <= c["list_seq"]:
            raise bad
    b = obj["burned"]
    if type(b) is not dict or set(b) != {"epochs", "list_seqs"} or type(b["epochs"]) is not dict:
        raise bad
    for k, named in b["epochs"].items():
        if (
            type(k) is not str
            or not _DEC.fullmatch(k)
            or not _int(int(k), 1, ehw)
            or not _int(named, int(k) + 1, MAX_EPOCH)
        ):
            raise bad
    if type(b["list_seqs"]) is not list or any(not _int(s, 1, lhw) for s in b["list_seqs"]):
        raise bad
    g = obj["gate"]
    if g is not None:
        if type(g) is not dict or set(g) != _GATE or type(g["up"]) is not bool or type(g["reasons"]) is not list:
            raise bad
        if not _int(g["since_ms"], 0, MAX_SAFE_INT) or (
            g["target"] is not None and not _int(g["target"], 1, MAX_EPOCH)
        ):
            raise bad
        for r in g["reasons"]:
            if type(r) is not dict or set(r) != {"device", "reason"} or r["reason"] not in GATE_REASONS:
                raise bad
            if type(r["device"]) is not str or not (_HEX32.fullmatch(r["device"]) or r["device"] == ""):
                raise bad
    for k in ("holders", "excluded", "h8"):
        if type(obj[k]) is not dict:
            raise bad
    if obj["unmatched_rotation_ms"] is not None and not _int(obj["unmatched_rotation_ms"], 0, MAX_SAFE_INT):
        raise bad
    card = obj["card"]
    if type(card) is not dict or set(card) != _CARD_NUMS or type(card["wxk_retired"]) is not list:
        raise bad
    for k in ("card_seq", "wxk_version", "ck_seq"):
        if not _int(card[k], 0, MAX_EPOCH):
            raise bad
    if card["pending_card_seq"] is not None and not _int(card["pending_card_seq"], 1, MAX_EPOCH):
        raise bad
    return obj


def _doc(obj: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(obj), sort_keys=True, separators=(",", ":")).encode()


def _parse_key(raw: bytes | None, kind: str, n: int, field: str) -> bytes:
    """The 32-byte secret of ``keys/<kind>-<n>.json`` (``wk``: ``epoch``, ``ck``: ``card_seq``); anything else is
    ``key_unreadable``."""
    nfield = {"wk": "epoch", "ck": "card_seq"}[kind]
    try:
        obj = json.loads(raw) if raw is not None else None
        if type(obj) is not dict or set(obj) != {"v", nfield, field, "created_ms"} or obj["v"] != 1:
            raise ValueError
        if obj[nfield] != n or type(obj[nfield]) is not int:
            raise ValueError
        return crypto.unb64u(obj[field], 32)
    except (ValueError, crypto.EncodingError, TypeError):
        raise MembershipUnsafe("key_unreadable") from None


class MembershipKeys:
    """R6's key custody and seal state for one workspace (11A.15.3): implements R2's ``EpochKeySource``
    (``current_epoch``, ``wk``, ``refusal_key``, ``seal_state``, ``seal_snapshot``, ``serialised``).

    ``root`` is ``<state>/hosts/<ws>/membership`` (resolved; ``hosts/<ws>`` must exist). One live instance per
    directory (``StateDir``'s ``flock``); a forked child refuses every operation. ``fault(point)`` is the crash seam
    of the tests (called after every durable step)."""

    def __init__(
        self,
        root: str | Path,
        workspace_id: str,
        *,
        now_ms: Callable[[], int],
        fault: Callable[[str], None] | None = None,
    ) -> None:
        crypto.unhex(workspace_id, 16)
        self.workspace_id = workspace_id
        self._now_ms = now_ms
        self._fault = fault or (lambda point: None)
        self._seal = threading.RLock()  # the seal lock (11A.15.2): in-memory seal state only, never any I/O
        self._writer = threading.RLock()  # R6's one head.json writer (taken before the seal lock, never inside it)
        self._dir = StateDir(root)
        try:
            self._keys = StateDir(Path(root) / "keys")
            try:
                self._cards = StateDir(Path(root) / "cards")
            except BaseException:
                self._keys.close()
                raise
        except BaseException:
            self._dir.close()
            raise
        self.ledger = FileSignLedger(self._dir)
        # the in-memory seal state, guarded by the seal lock
        self._wk: dict[int, bytes] = {}
        self._ec = 0
        self._gate_up = False
        self._gate_queue: list[dict[str, str]] = []
        self._burned: dict[int, int] = {}
        self._pending: int | None = None
        self._ehw = 0
        self._lhw = 0
        self._unsafe: str | None = None
        self._head: dict[str, Any] = _fresh_head()
        try:
            with self._writer:
                self._load()
        except BaseException:  # a failed (or crashed) load never keeps the directories locked
            self.close()
            raise

    def __repr__(self) -> str:
        return f"MembershipKeys(workspace={self.workspace_id}, epoch={self._ec})"

    def close(self) -> None:
        self._cards.close()
        self._keys.close()
        self._dir.close()

    # -- load and recovery -------------------------------------------------------------------------------------------

    def _numbers(self, d: StateDir, pattern: re.Pattern[str]) -> list[int]:
        return sorted(int(m.group(1)) for n in d.names() if (m := pattern.fullmatch(n)))

    def _read_key(self, kind: str, n: int, field: str) -> bytes:
        try:
            raw = self._keys.read(f"{kind}-{n}.json")
        except StateError:  # a symlink, another owner, a wider mode: refused, never repaired
            raise MembershipUnsafe("key_unreadable") from None
        return _parse_key(raw, kind, n, field)

    def _scan_wk(self) -> dict[int, bytes]:
        return {e: self._read_key("wk", e, "wk") for e in self._numbers(self._keys, _WK)}

    def _ledger_numbers(self, kind: str) -> list[int]:
        return [int(k) for k in self.ledger.keys(kind)]

    def _unsafe_now(self, code: str) -> None:
        with self._seal:
            self._unsafe = code
            self._wk, self._ec, self._gate_up, self._pending = {}, 0, True, None

    def _load(self) -> None:
        for d in (self._dir, self._keys, self._cards):
            d.discard_temps()  # a crashed claim's temp file may hold a key: never left behind
        wk_epochs = self._numbers(self._keys, _WK)
        try:
            ledger_epochs = self._ledger_numbers("wk_commit")
            ledger_seqs = self._ledger_numbers("member_list")
            ledger_cards = self._ledger_numbers("card")
        except (StateError, ValueError):
            return self._unsafe_now("ledger_unreadable")
        self._ehw = max([0, *wk_epochs, *ledger_epochs])
        self._lhw = max([0, *ledger_seqs])
        try:
            head = self._dir.read_json(HEAD)
        except StateError:
            return self._unsafe_now("head_unreadable")
        if head is None:
            other = self._keys.names() or self._cards.names() or ledger_seqs or ledger_epochs or ledger_cards
            if other:
                return self._unsafe_now("head_lost")
            head = _fresh_head()
            self._dir.write_json(HEAD, head)
            self._fault("after_head_create")
        try:
            head = _check_head(head)
            held = self._scan_wk()
        except MembershipUnsafe as e:
            return self._unsafe_now(e.code)
        committed, exposed = head["committed"], head["exposed"]
        ec = committed["epoch"] if committed else 0
        if committed is not None:
            wk = held.get(ec)
            if wk is None:
                return self._unsafe_now("key_missing")
            if not hmac.compare_digest(
                pairing.wk_commit(self.workspace_id, ec, wk).encode(), committed["wk_commit"].encode()
            ):
                return self._unsafe_now("wk_commit_mismatch")
        for e, wk in held.items():  # every held key against the commit the signer recorded for its epoch
            rec = self.ledger.get("wk_commit", str(e))
            if rec is not None and not hmac.compare_digest(
                rec[0].encode(), pairing.wk_commit(self.workspace_id, e, wk).encode()
            ):
                return self._unsafe_now("wk_commit_mismatch")
        pending = exposed["new_epoch"] if exposed is not None else None
        if pending is not None and pending not in held:
            return self._unsafe_now("key_missing")
        burned = {int(k): v for k, v in head["burned"]["epochs"].items()}
        ehw = max(head["ehw"], self._ehw)
        lhw = max(head["lhw"], self._lhw)
        orphans = [e for e in held if e > ec and e not in burned and e != pending]
        for e in orphans:  # the orphan rule: claimed, never exposed: burned, never activated or generated again
            burned[e] = min(ehw + 1, MAX_EPOCH)
        if orphans or ehw != head["ehw"] or lhw != head["lhw"]:
            head = {**head, "ehw": ehw, "lhw": lhw,
                    "burned": {"epochs": {str(e): n for e, n in sorted(burned.items())},
                               "list_seqs": head["burned"]["list_seqs"]}}  # fmt: skip
            self._dir.write_json(HEAD, head)  # durable before anything is served
            self._fault("after_recovery")
        self._head = head
        gate = head["gate"]
        with self._seal:
            self._wk, self._ec, self._burned, self._pending = held, ec, burned, pending
            self._ehw, self._lhw = ehw, lhw
            self._gate_up = gate is not None and gate["up"]
            self._unsafe = None
        return None

    @property
    def unsafe(self) -> str | None:
        """The H7 cause (``head_lost``, ``head_unreadable``, ``head_shape``, ``key_missing``, ``key_unreadable``,
        ``wk_commit_mismatch``, ``ledger_unreadable``) or ``None``."""
        with self._seal:
            return self._unsafe

    def check_start(self) -> None:
        """Before the worker starts (11A.15.2): :class:`MembershipUnsafe` while the state is unsafe."""
        code = self.unsafe
        if code is not None:
            raise MembershipUnsafe(code)

    def _require_safe(self) -> None:
        self.check_start()

    def recover_h7(self, *, relay_lhw: int, relay_ehw: int) -> None:
        """§11 H7 from R6's side (11A.15.4), the local half: R6b calls it after a verified ``GET members`` with that
        list's ``list_seq`` and ``epoch`` (0 when the relay holds none). ``lhw``/``ehw`` are raised by 1024 above the
        larger of the relay's, every local key file, every ledger entry and the old head; every held key becomes
        burned (nothing is committed any more: the next list is a rotation above them), the gate is up (``h7``) and
        the card numbers are rebuilt from ``cards/`` and ``keys/`` (pending, never accepted). A head that was
        readable keeps its ``holders``/``excluded``; R6b then rotates at once."""
        if not _int(relay_lhw, 0, MAX_SAFE_INT) or not _int(relay_ehw, 0, MAX_EPOCH):
            raise CustodyRefused("relay high-water marks")
        with self._writer:
            for d in (self._dir, self._keys, self._cards):
                d.discard_temps()
            wk_epochs = self._numbers(self._keys, _WK)
            try:
                ledger_epochs = self._ledger_numbers("wk_commit")
                ledger_seqs = self._ledger_numbers("member_list")
                ledger_cards = self._ledger_numbers("card")
            except (StateError, ValueError):
                raise MembershipUnsafe("ledger_unreadable") from None
            try:
                old = _check_head(self._dir.read_json(HEAD))
            except (StateError, MembershipUnsafe):
                old = None
            lhw = max([relay_lhw, *ledger_seqs, old["lhw"] if old else 0, self._lhw]) + H7_MARGIN
            ehw = max([relay_ehw, *wk_epochs, *ledger_epochs, old["ehw"] if old else 0, self._ehw]) + H7_MARGIN
            if ehw > MAX_EPOCH:
                raise CustodyRefused("epoch space exhausted")
            head = _fresh_head()
            if old is not None:
                head["holders"], head["excluded"] = old["holders"], old["excluded"]
            burned = {e: ehw + 1 for e in wk_epochs}
            if old is not None:
                burned.update({int(k): v for k, v in old["burned"]["epochs"].items()})
            cards = self._numbers(self._cards, _CARD)
            wxk = self._numbers(self._keys, _WXK)
            cks = self._numbers(self._keys, _CK)
            head.update(
                lhw=lhw, ehw=ehw,
                burned={"epochs": {str(e): n for e, n in sorted(burned.items())}, "list_seqs": []},
                gate={"up": True, "reasons": [{"device": "", "reason": "h7"}], "since_ms": self._now_ms(),
                      "target": min(ehw + 1, MAX_EPOCH)},
                card={"card_seq": 0, "wxk_version": max([0, *wxk]), "ck_seq": max([0, *cks]),
                      "pending_card_seq": max(cards) if cards else None, "wxk_retired": []},
            )  # fmt: skip
            del ledger_cards
            self._dir.write_json(HEAD, head)
            self._fault("after_h7")
            held = {}
            for e in wk_epochs:
                try:
                    held[e] = self._read_key("wk", e, "wk")
                except MembershipUnsafe:
                    pass  # an unreadable key is burned and never served
            self._head = head
            with self._seal:
                self._wk, self._ec, self._burned, self._pending = held, 0, burned, None
                self._ehw, self._lhw, self._gate_up, self._unsafe = ehw, lhw, True, None

    # -- R2's EpochKeySource ----------------------------------------------------------------------------------------

    def current_epoch(self) -> int:
        """The committed epoch ``e_c`` (0 before the first committed list or while unsafe). Informational only."""
        with self._seal:
            return self._ec

    def wk(self, epoch: int) -> bytes | None:
        """The key of a held epoch ``1 <= e <= e_c`` (active, old, or burned below ``e_c``); ``None`` above ``e_c``
        (burned, exposed or pending), for an epoch never held, and while unsafe."""
        with self._seal:
            if type(epoch) is not int or not 1 <= epoch <= self._ec:
                return None
            return self._wk.get(epoch)

    def refusal_key(self, epoch: int) -> bytes | None:
        """A key for a refusal only (11A.15.2): an old epoch below ``e_c``, ``e_c`` while the gate is up, or a burned
        epoch above ``e_c`` (proto §8.5 G2b). ``None`` otherwise (an exposed or pending epoch, never held)."""
        with self._seal:
            if type(epoch) is not int or epoch < 1:
                return None
            if (
                epoch < self._ec
                or (epoch == self._ec and self._gate_up)
                or (epoch > self._ec and epoch in self._burned)
            ):
                return self._wk.get(epoch)
            return None

    def _target(self) -> int:
        """The epoch a ``stale_epoch`` names (C5): the exposed rotation's epoch, else ``EHW + 1``, at most the clamp
        (``e_c + 2^16``); call with the seal lock held."""
        if self._pending is not None:
            return self._pending
        t = self._ehw + 1
        if self._ec:
            t = min(t, self._ec + CLAMP)
        return min(t, MAX_EPOCH)

    def seal_state(self) -> SealState:
        """One snapshot: the committed epoch; ``gated`` when the gate is up, before the first commit and while
        unsafe (no data is sealed then); the epoch a ``stale_epoch`` names."""
        with self._seal:
            return SealState(self._ec, self._gate_up or self._ec < 1 or self._unsafe is not None, self._target())

    @contextlib.contextmanager
    def seal_snapshot(self) -> Iterator[SealState]:
        """The seal lock for the duration of one seal, and ONE :meth:`seal_state` taken under it."""
        with self._seal:
            yield self.seal_state()

    @contextlib.contextmanager
    def serialised(self) -> Iterator[None]:
        """The seal lock alone."""
        with self._seal:
            yield

    def is_burned(self, epoch: int) -> bool:
        """For the signer's epoch floor (``RelaySigner.member_list``): ``epoch`` is burned (an abandoned exposure or
        an orphan key)."""
        with self._seal:
            return type(epoch) is int and epoch in self._burned

    # -- the gate ---------------------------------------------------------------------------------------------------

    def raise_gate(self, device: str, reason: str) -> None:
        """The in-memory gate, at once and under the seal lock only (called by :class:`R6Gate` inside the store's
        critical section); the durable write is queued for :meth:`flush_gate`. No I/O, no other lock."""
        if type(device) is not str or not (_HEX32.fullmatch(device) or device == ""):
            raise ValueError("gate device")
        if reason not in GATE_REASONS:
            raise ValueError("gate reason")
        with self._seal:
            self._gate_up = True
            self._gate_queue.append({"device": device, "reason": reason})

    def gate_up(self) -> bool:
        with self._seal:
            return self._gate_up

    def flush_gate(self) -> bool:
        """R6's thread, before any publication work: write every queued gate reason into ``head.json`` (atomic).
        ``True`` when something was written. The in-memory gate stays up whatever happens here."""
        with self._writer:
            with self._seal:
                queued = list(self._gate_queue)
                target = self._target()
            if not queued:
                return False
            head = self._head
            gate = head["gate"] or {"up": True, "reasons": [], "since_ms": self._now_ms(), "target": None}
            reasons = list(gate["reasons"])
            for q in queued:
                if q not in reasons:
                    reasons.append(q)
            new = {**head, "gate": {**gate, "up": True, "reasons": reasons, "target": target}}
            self._dir.write_json(HEAD, new)
            self._fault("after_gate")
            self._head = new
            with self._seal:
                del self._gate_queue[: len(queued)]
            return True

    # -- epoch keys and the head transitions R6b drives (11A.15.4 write-ahead) ----------------------------------------

    def high_water(self) -> tuple[int, int]:
        """``(lhw, ehw)`` as recovered (head, key files, ledger) and raised since."""
        with self._seal:
            return self._lhw, self._ehw

    def new_epoch_key(self, epoch: int) -> str:
        """11A.15.4 step 2: draw ``WK_E`` and claim ``keys/wk-<E>.json`` (fsynced) **before** the ``head.json`` write
        that allocates ``E``; returns ``wk_commit(ws, E, WK_E)``, never the key. Refused: an epoch at or below the
        high-water mark (never reused), above the clamp (``e_c + 2^16``; before any list, ``2^16``), an existing
        file, and while unsafe. The in-memory floor moves at once; until :meth:`expose` names it, the key is an
        orphan (burned at the next load)."""
        self._require_safe()
        with self._writer:
            if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
                raise CustodyRefused("epoch range")
            with self._seal:
                ec, ehw, lhw = self._ec, self._ehw, self._lhw
            if epoch <= ehw:
                raise CustodyRefused("epoch at or below the high-water mark: an epoch is never reused")
            limit = ec + CLAMP if ec else (FIRST_EPOCH_MAX if lhw == 0 else MAX_EPOCH)
            if epoch > min(limit, MAX_EPOCH):
                raise CustodyRefused("epoch above the clamp")
            name = f"wk-{epoch}.json"
            wk = secrets.token_bytes(32)
            self._fault("before_wk_claim")
            if not self._keys.claim(
                name, _doc({"v": 1, "epoch": epoch, "wk": crypto.b64u(wk), "created_ms": self._now_ms()})
            ):
                raise CustodyRefused("a key file for this epoch exists: an epoch is never reused")
            self._fault("after_wk_claim")
            with self._seal:
                self._wk[epoch] = wk
                self._ehw = max(self._ehw, epoch)
            return pairing.wk_commit(self.workspace_id, epoch, wk)

    def wk_commit_of(self, epoch: int) -> str | None:
        """``wk_commit`` of a held epoch (any), for R6b's planner; never the key."""
        with self._seal:
            wk = self._wk.get(epoch) if type(epoch) is int else None
        return None if wk is None else pairing.wk_commit(self.workspace_id, epoch, wk)

    def head(self) -> dict[str, Any]:
        """A deep copy of ``head.json`` (no secrets in it)."""
        with self._writer:
            return json.loads(_doc(self._head))

    def expose(self, *, list_seq: int, epoch: int, body: bytes, digest: str, adds: Sequence[str]) -> None:
        """11A.15.4 step 4, the ONE atomic ``head.json`` write before the first send: ``lhw := list_seq``; for a
        rotation (``epoch != e_c``) ``ehw := max(ehw, epoch)`` with the key already claimed; ``exposed`` = the exact
        body; a body exposed before is superseded (H4): its ``list_seq`` is burned, and its epoch too if it
        introduced one, naming this body's epoch when it is higher, else ``EHW + 1``."""
        self._require_safe()
        if type(body) is not bytes or not body or type(digest) is not str:
            raise CustodyRefused("exposed body")
        if isinstance(adds, (str, bytes)) or any(type(a) is not str or not _HEX32.fullmatch(a) for a in adds):
            raise CustodyRefused("adds")
        with self._writer:
            with self._seal:
                ec, ehw, lhw = self._ec, self._ehw, self._lhw
                held = epoch in self._wk if type(epoch) is int else False
                burned_now = dict(self._burned)
            if not _int(list_seq, lhw + 1, MAX_SAFE_INT):
                raise CustodyRefused("list_seq at or below the high-water mark")
            rotation = epoch != ec
            if rotation and (not _int(epoch, ec + 1, MAX_EPOCH) or not held or epoch in burned_now):
                raise CustodyRefused("rotation epoch: above e_c, claimed, not burned")
            if not rotation and ec < 1:
                raise CustodyRefused("the first list is a rotation")
            head = self._head
            burned = {int(k): v for k, v in head["burned"]["epochs"].items()}
            seqs = list(head["burned"]["list_seqs"])
            old = head["exposed"]
            if old is not None:
                seqs.append(old["list_seq"])
                oe = old["new_epoch"]
                if oe is not None:
                    burned[oe] = epoch if rotation and epoch > oe else min(max(ehw, epoch) + 1, MAX_EPOCH)
            new = {
                **head,
                "lhw": list_seq,
                "ehw": max(ehw, epoch),
                "exposed": {"list_seq": list_seq, "epoch": epoch, "new_epoch": epoch if rotation else None,
                            "body": crypto.b64u(body), "digest": digest, "adds": sorted(set(adds)),
                            "first_send_ms": None, "attempts": 0},
                "burned": {"epochs": {str(e): n for e, n in sorted(burned.items())}, "list_seqs": sorted(set(seqs))},
            }  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._fault("after_expose")
            self._head = new
            with self._seal:
                self._lhw, self._ehw = list_seq, max(self._ehw, epoch)
                self._burned = burned
                self._pending = epoch if rotation else None

    def commit(self, *, list_seq: int, epoch: int, list_hash: str, list_obj: Mapping[str, Any], at_ms: int) -> bool:
        """The 2xx of the exposed body (H3): one atomic write sets ``committed``, clears ``exposed`` and lowers the
        gate when this is a rotation that leaves out every gated device (and no raise is still queued); then, under
        the seal lock, ``e_c`` moves and the in-memory gate is lowered, never before the durable write. ``False``
        (nothing written) for anything but the exposed body: a late 2xx for an abandoned body never activates a
        burned epoch."""
        self._require_safe()
        with self._writer:
            ex = self._head["exposed"]
            if ex is None or ex["list_seq"] != list_seq or ex["epoch"] != epoch:
                return False
            with self._seal:
                wk, ec, queued = self._wk.get(epoch), self._ec, bool(self._gate_queue)
            if wk is None:
                raise MembershipUnsafe("key_missing")
            commit = pairing.wk_commit(self.workspace_id, epoch, wk)
            members = list_obj.get("members") if isinstance(list_obj, Mapping) else None
            if type(members) is not list or list_obj.get("epoch") != epoch or list_obj.get("wk_commit") != commit:
                raise CustodyRefused("the committed list is not the exposed body's")
            ids = {m.get("device_id") for m in members if isinstance(m, Mapping)}
            gate = self._head["gate"]
            lower = (
                gate is not None and epoch > ec and not queued and not any(r["device"] in ids for r in gate["reasons"])
            )
            new = {
                **self._head,
                "committed": {"list_seq": list_seq, "epoch": epoch, "list_hash": list_hash, "wk_commit": commit,
                              "list": json.loads(_doc(list_obj)), "at_ms": at_ms},
                "exposed": None,
                "gate": None if lower else gate,
            }  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._fault("after_commit")
            self._head = new
            with self._seal:
                self._ec, self._pending = epoch, None
                if lower and not self._gate_queue:
                    self._gate_up = False
            return True

    def abandon(self) -> None:
        """Abandon the exposed body (H4: a content or divergence refusal): its ``list_seq`` is burned, and its epoch
        if it introduced one (named ``EHW + 1``). One atomic write."""
        with self._writer:
            ex = self._head["exposed"]
            if ex is None:
                return
            with self._seal:
                ehw = self._ehw
            burned = {int(k): v for k, v in self._head["burned"]["epochs"].items()}
            if ex["new_epoch"] is not None:
                burned[ex["new_epoch"]] = min(ehw + 1, MAX_EPOCH)
            seqs = sorted({*self._head["burned"]["list_seqs"], ex["list_seq"]})
            new = {**self._head, "exposed": None,
                   "burned": {"epochs": {str(e): n for e, n in sorted(burned.items())}, "list_seqs": seqs}}  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._fault("after_abandon")
            self._head = new
            with self._seal:
                self._burned, self._pending = burned, None

    # -- the first card (11A.15.5, C6) -----------------------------------------------------------------------------

    def _read_card(self, s: int) -> dict[str, Any]:
        try:
            raw = self._cards.read(f"card-{s}.json")
        except StateError:
            raise MembershipUnsafe("card_unreadable") from None
        if raw is None:
            raise MembershipUnsafe("card_missing")
        try:
            obj = canon.loads_strict(raw)
        except ValueError:
            raise MembershipUnsafe("card_unreadable") from None
        if type(obj) is not dict:
            raise MembershipUnsafe("card_unreadable")
        return obj

    def first_card(
        self,
        sign_card: Callable[[Mapping[str, Any]], str],
        *,
        delegation: Mapping[str, Any],
        owner_pk_pub: bytes,
        wsk_pub: bytes,
        relay_url: str,
        part: Mapping[str, Any],
        issued_ms: int,
    ) -> dict[str, Any]:
        """The first card (R1's ``LinkSteps.card()``), identical on every call. In order, each step durable before
        the next: claim ``keys/wxk-<v>.json``, claim ``keys/ck-<s>.json``, sign (``sign_card`` is
        ``RelaySigner.card``: its ledger entry is durable before the signature returns), verify the full card under
        the owner's ``PK`` (``wscard.verify_card``), claim ``cards/card-<s>.json``, then ``head.json`` ``card``. A
        stored card is returned as stored (verified again). An orphan ``wxk``/``ck`` file, or a signed card whose file
        was never claimed, is never reused: the next attempt draws ``v``/``s`` above every file, every ledger card
        and the head (so the first card may carry ``card_seq`` above 1 after a crash)."""
        self._require_safe()
        with self._writer:
            card = self._head["card"]
            stored = card["pending_card_seq"] or card["card_seq"] or None
            files = self._numbers(self._cards, _CARD)
            if stored is None and files:
                stored = files[-1]  # claimed, head not yet written (a crash): resume with those bytes
            if stored is not None:
                c = self._read_card(stored)
                self._verified(c, owner_pk_pub, stored)
                if card["pending_card_seq"] is None and card["card_seq"] == 0:
                    o = c["o"]
                    self._write_card(stored, o["wxk_version"])
                return c
            try:
                ledger_cards = self._ledger_numbers("card")
            except (StateError, ValueError):
                raise MembershipUnsafe("ledger_unreadable") from None
            v = max([card["wxk_version"], *self._numbers(self._keys, _WXK)]) + 1
            s = max([card["card_seq"], card["ck_seq"], *self._numbers(self._keys, _CK), *files, *ledger_cards]) + 1
            if s > MAX_EPOCH:
                raise CustodyRefused("card_seq space exhausted")
            now = self._now_ms()
            wxk = crypto.generate_private_key()
            wxk_pub = crypto.public_bytes(wxk)
            doc = {"v": 1, "version": v, "d": crypto.b64u(crypto.private_scalar(wxk)), "pub": crypto.b64u(wxk_pub),
                   "created_ms": now}  # fmt: skip
            del wxk
            if not self._keys.claim(f"wxk-{v}.json", _doc(doc)):
                raise CustodyRefused("a WXK file for this version exists: never reused")
            self._fault("after_wxk_claim")
            ck = secrets.token_bytes(32)
            if not self._keys.claim(
                f"ck-{s}.json", _doc({"v": 1, "card_seq": s, "ck": crypto.b64u(ck), "created_ms": now})
            ):
                raise CustodyRefused("a CK file for this card_seq exists: never reused")
            self._fault("after_ck_claim")
            try:
                o, sealed = wscard.card_o(
                    workspace_id=self.workspace_id, wsk_pub=wsk_pub, wxk_pub=wxk_pub, wxk_version=v,
                    owner_person_id=crypto.person_id(owner_pk_pub).hex(), relay_url=relay_url, card_seq=s,
                    issued_ms=issued_ms, ck=ck, part=dict(part),
                )  # fmt: skip
            except Refused as e:
                raise CustodyRefused(f"card: {e}") from None
            sig = sign_card(o)
            self._fault("after_sign")
            full = {"delegation": dict(delegation), "o": o, "sig": sig, "sealed": crypto.b64u(sealed)}
            self._verified(full, owner_pk_pub, s)
            if not self._cards.claim(f"card-{s}.json", canon.dumps(full)):
                full = self._read_card(s)
                self._verified(full, owner_pk_pub, s)
            self._fault("after_card_claim")
            self._write_card(s, v)
            return full

    def _verified(self, c: Mapping[str, Any], owner_pk_pub: bytes, s: int) -> None:
        try:
            vc = wscard.verify_card(dict(c), owner_pk_pub)
        except Refused as e:
            raise CustodyRefused(f"the card does not verify: {e.code}") from None
        if vc.workspace_id != self.workspace_id or vc.card_seq != s:
            raise CustodyRefused("the card names another workspace or card_seq")

    def _write_card(self, s: int, v: int) -> None:
        card = {**self._head["card"], "pending_card_seq": s, "wxk_version": max(v, self._head["card"]["wxk_version"]),
                "ck_seq": max(s, self._head["card"]["ck_seq"])}  # fmt: skip
        new = {**self._head, "card": card}
        self._dir.write_json(HEAD, new)
        self._fault("after_card_head")
        self._head = new

    def card_accepted(self, card_seq: int) -> None:
        """The relay accepted ``card_seq`` (R1/R5 after the first ``PUT card``, R6d after a rotation)."""
        with self._writer:
            card = self._head["card"]
            if card["pending_card_seq"] != card_seq:
                raise CustodyRefused("not the pending card")
            new = {**self._head, "card": {**card, "card_seq": card_seq, "pending_card_seq": None}}
            self._dir.write_json(HEAD, new)
            self._head = new

    def card_key(self, card_seq: int) -> bytes:
        """``CK_s`` of a stored card (for R6b's card-key wraps); refused for a card_seq without a claimed card."""
        with self._writer:
            if not _int(card_seq, 1, MAX_EPOCH) or f"card-{card_seq}.json" not in self._cards.names():
                raise CustodyRefused("no card at this card_seq")
            return self._read_key("ck", card_seq, "ck")


class R6Gate:
    """The store's hook (11A.15.2): ``raise_for(device, reason)`` inside the store's critical section that appends
    F1 ``device.removed``/``device.revoked`` or holds forwarded evidence, before the store lock is released. It takes
    the seal lock (store lock, then seal lock), raises the in-memory gate and queues the durable write; it never does
    I/O, never takes the store lock, and a seal that began before it finished first (it waited for the seal lock)."""

    __slots__ = ("_keys",)

    def __init__(self, keys: MembershipKeys) -> None:
        self._keys = keys

    def raise_for(self, device: str, reason: str) -> None:
        self._keys.raise_gate(device, reason)

    def on_commit(self, log: str, event: Mapping[str, Any]) -> None:
        """A ``Store.on_commit``-shaped hook: ``device.removed``/``device.revoked`` of the workspace log raise the gate
        for that device (its id without the ``d_`` prefix). Every other event is ignored."""
        typ = event.get("type") if isinstance(event, Mapping) else None
        if typ not in ("device.removed", "device.revoked"):
            return
        dev = event.get("device")
        if type(dev) is str and dev.startswith("d_") and _HEX32.fullmatch(dev[2:]):
            self.raise_for(dev[2:], typ)
        else:
            self.raise_for("", typ)  # unparseable: fail closed, the gate goes up for every device
