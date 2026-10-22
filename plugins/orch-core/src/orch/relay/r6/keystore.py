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

**Locks, ONE order (round 2, F5):** the store lock, then the writer lock (R6's single ``head.json`` writer: every
method that writes takes it), then the seal lock (11A.15.2), then R2's RidStore lock. The seal lock guards the
in-memory seal state; R6 holds it only for memory updates, but R2 holds it across a seal and the commit of the
replayable bytes (``rids.answer``: SQLite with fsync, and the WSK signature), so a gate raise waits for that I/O
(availability, not safety). **Never call a writer method inside** :meth:`MembershipKeys.serialised` or a seal span:
it is refused at once (``RuntimeError``), never a deadlock (a thread-local count of seal-lock holds is checked before
the writer lock is taken).

**The gate is durable before** :meth:`R6Gate.raise_for` **returns** (round 2, F4): the in-memory gate goes up under
the seal lock, then the raise is written into ``head.json`` (writer lock, after the seal lock is released). The store
calls the hook through ``Store.relay_gate`` (installed by :meth:`R6Gate.install`), which the host's journal never
replaces; until the hook is installed the store is **gated** (fail closed). A crash between the store's commit and
the durable write is closed at load by the host (``relayr6.r6``: every member of the committed list whose F1 standing
is not ``ok`` raises the gate before the worker starts).

**A forked child** (``os.fork``) serves nothing: every method checks the pid before it takes any lock (a lock held by
another parent thread at the fork is never released in the child) and answers fail closed (no key, gated) or raises.

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
import logging
import os
import re
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from orch import canon, crypto
from orch.identity import memberlist, pairing, wscard
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
LOCAL_MARK = "local-epochs.json"  # advance_local ran: the workspace has epochs no relay knows (V3 C2b)
PRIMARY = "primary.json"  # R6c: the verified primary's certificate and the human's label (11A.15.1)
log = logging.getLogger("orch.relay.r6")
GATE_REASONS = frozenset(
    {"device.removed", "device.revoked", "held_evidence", "relay_revocation", "revoked_refusal", "rotation_due",
     "h7", "rotate", "f1_standing"}
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


def _list_of(body: bytes) -> dict[str, Any]:
    """The member list ``o`` of an exposed body: the body is either ``cj(o)`` or the ``PUT membership`` body
    ``{member_list: {o, sig}, ...}``; anything else is refused."""
    try:
        obj = canon.loads_strict(body)
    except ValueError:
        raise CustodyRefused("exposed body is not canonical JSON") from None
    if type(obj) is dict and type(obj.get("member_list")) is dict:
        obj = obj["member_list"].get("o")
    if type(obj) is not dict or obj.get("kind") != "member_list" or type(obj.get("members")) is not list:
        raise CustodyRefused("exposed body holds no member list")
    return obj


def _list_digest(body: bytes) -> str:
    """``list_hash`` of an exposed body: ``"sha256:" + hex(sha256(cj(signed list)))`` for a ``PUT membership`` body,
    ``"sha256:" + hex(sha256(cj(o)))`` for a bare list ``o``."""
    obj = canon.loads_strict(body)
    if type(obj) is dict and type(obj.get("member_list")) is dict:
        obj = obj["member_list"]
    return "sha256:" + crypto.sha256(canon.dumps(obj)).hex()


class _SealLock:
    """The seal lock: an ``RLock`` that knows whether the current thread holds it (the lock-order check of the
    writer methods, F5)."""

    __slots__ = ("_holds", "_lock")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._holds: dict[int, int] = {}  # thread id -> hold count; written only by the holding thread

    def __enter__(self) -> None:
        self._lock.acquire()
        me = threading.get_ident()
        self._holds[me] = self._holds.get(me, 0) + 1

    def __exit__(self, *exc: object) -> None:
        me = threading.get_ident()
        n = self._holds[me] - 1
        if n:
            self._holds[me] = n
        else:
            del self._holds[me]
        self._lock.release()

    def held(self) -> bool:
        return self._holds.get(threading.get_ident(), 0) > 0


class MembershipKeys:
    """R6's key custody and seal state for one workspace (11A.15.3): implements R2's ``EpochKeySource``
    (``current_epoch``, ``wk``, ``refusal_key``, ``seal_state``, ``seal_snapshot``, ``serialised``).

    ``root`` is ``<state>/hosts/<ws>/membership`` (resolved; ``hosts/<ws>`` must exist). One live instance per
    directory (``StateDir``'s ``flock``); a forked child serves nothing (fail closed). ``fault(point)`` is the crash
    seam of the tests (called after every durable step). ``previously_linked`` (F2): the workspace is linked (or has
    a first card at the relay), so a missing ``head.json`` is H7 even when the whole directory is gone."""

    def __init__(
        self,
        root: str | Path,
        workspace_id: str,
        *,
        now_ms: Callable[[], int],
        fault: Callable[[str], None] | None = None,
        previously_linked: bool = False,
    ) -> None:
        crypto.unhex(workspace_id, 16)
        self._pid = os.getpid()
        self._previously_linked = previously_linked is True
        #: the host's fresh "is a relay linked now?" (``relayr6.keys_for`` sets it); consulted by every local epoch
        self.link_probe: Callable[[], bool] | None = None
        self.workspace_id = workspace_id
        self.root = Path(root)
        self._now_ms = now_ms
        self._fault = fault or (lambda point: None)
        self._seal = _SealLock()  # the seal lock (11A.15.2)
        self._writer = threading.RLock()  # R6's one head.json writer (taken before the seal lock, never inside it)
        # the store hook, bound to the LIVE store (round 3, R3): gated unless live() is a store whose relay_gate is
        # exactly the callable R6Gate.install put there; checked at every seal_state, never a latch
        self._live: Callable[[], Any] = lambda: None
        self._hook: object = None
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
            with self._write():
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

    def _forked(self) -> bool:
        return os.getpid() != self._pid

    @contextlib.contextmanager
    def _write(self) -> Iterator[None]:
        """The writer lock, in the one order (F5): refused inside a seal-lock hold of this thread, and in a forked
        child."""
        if self._forked():
            raise MembershipUnsafe("forked")
        if self._seal.held():
            raise RuntimeError("lock order: a membership writer method inside the seal lock (serialised/a seal span)")
        with self._writer:
            yield

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
            if other or self._previously_linked:  # F2: a lost directory of a linked workspace is H7, never fresh
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
        # F1: the ledger says an epoch above e_c was signed (or a list above the head's LHW) that this head does not
        # account for: a rolled-back head (a restore) or the crash window between signing and the expose write. Not
        # provably an orphan, so the store starts GATED (durable, reason h7) until R6b's verified GET members.
        unaccounted = [e for e in ledger_epochs if e > ec and e not in burned and e != pending] or [
            q for q in ledger_seqs if q > head["lhw"]
        ]
        orphans = [e for e in held if e > ec and e not in burned and e != pending]
        for e in orphans:  # the orphan rule: claimed, never exposed: burned, never activated or generated again
            burned[e] = min(ehw + 1, MAX_EPOCH)
        gate = head["gate"]
        if unaccounted or orphans:  # an orphan key above e_c too (round 3, R2: head + ledger restored, keys newer)
            reason = {"device": "", "reason": "h7"}
            old = gate or {"up": True, "reasons": [], "since_ms": self._now_ms(), "target": None}
            reasons = old["reasons"] if reason in old["reasons"] else [*old["reasons"], reason]
            gate = {**old, "up": True, "reasons": reasons, "target": min(ehw + 1, MAX_EPOCH)}
        if orphans or unaccounted or ehw != head["ehw"] or lhw != head["lhw"]:
            head = {**head, "ehw": ehw, "lhw": lhw, "gate": gate,
                    "burned": {"epochs": {str(e): n for e, n in sorted(burned.items())},
                               "list_seqs": head["burned"]["list_seqs"]}}  # fmt: skip
            self._dir.write_json(HEAD, head)  # durable before anything is served
            self._head = head
            self._fault("after_recovery")
        self._head = head
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
        with self._write():
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
            self._head = head
            self._fault("after_h7")
            held = {}
            for e in wk_epochs:
                try:
                    held[e] = self._read_key("wk", e, "wk")
                except MembershipUnsafe:
                    pass  # an unreadable key is burned and never served
            with self._seal:
                self._wk, self._ec, self._burned, self._pending = held, 0, burned, None
                self._ehw, self._lhw, self._gate_up, self._unsafe = ehw, lhw, True, None

    # -- R2's EpochKeySource ----------------------------------------------------------------------------------------

    def current_epoch(self) -> int:
        """The committed epoch ``e_c`` (0 before the first committed list or while unsafe). Informational only."""
        if self._forked():
            return 0
        with self._seal:
            return self._ec

    def wk(self, epoch: int) -> bytes | None:
        """The key of a held epoch ``1 <= e <= e_c`` (active, old, or burned below ``e_c``); ``None`` above ``e_c``
        (burned, exposed or pending), for an epoch never held, and while unsafe."""
        if self._forked():
            return None
        with self._seal:
            if type(epoch) is not int or not 1 <= epoch <= self._ec:
                return None
            return self._wk.get(epoch)

    def refusal_key(self, epoch: int) -> bytes | None:
        """A key for a refusal only (11A.15.2): an old epoch below ``e_c``, ``e_c`` while the gate is up, or a burned
        epoch above ``e_c`` (proto §8.5 G2b). ``None`` otherwise (an exposed or pending epoch, never held)."""
        if self._forked():
            return None
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
        """One snapshot: the committed epoch; ``gated`` when the gate is up, before the first commit, while unsafe
        and until the store hook is installed (no data is sealed then); the epoch a ``stale_epoch`` names."""
        if self._forked():
            return SealState(0, True, 1)
        with self._seal:
            gated = self._gate_up or self._ec < 1 or self._unsafe is not None or not self._hooked()
            return SealState(self._ec, gated, self._target())

    @contextlib.contextmanager
    def seal_snapshot(self) -> Iterator[SealState]:
        """The seal lock for the duration of one seal, and ONE :meth:`seal_state` taken under it."""
        if self._forked():
            raise MembershipUnsafe("forked")
        with self._seal:
            yield self.seal_state()

    @contextlib.contextmanager
    def serialised(self) -> Iterator[None]:
        """The seal lock alone. Never call a writer method inside it (refused, F5)."""
        if self._forked():
            raise MembershipUnsafe("forked")
        with self._seal:
            yield

    def is_burned(self, epoch: int) -> bool:
        """For the signer's epoch floor (``RelaySigner.member_list``): ``epoch`` is burned (an abandoned exposure or
        an orphan key). In a forked child: every epoch counts as burned (fail closed)."""
        if self._forked():
            return True
        with self._seal:
            return type(epoch) is int and epoch in self._burned

    # -- the gate ---------------------------------------------------------------------------------------------------

    def raise_gate(self, device: str, reason: str) -> None:
        """The gate (called by :class:`R6Gate` inside the store's critical section): first the in-memory gate under
        the seal lock (a seal span in progress finishes first; the next one is gated), then, with the seal lock
        released, the durable write (:meth:`flush_gate`, writer lock: store, writer, seal). It is durable when this
        returns. A write that fails (a disk error) leaves the in-memory gate up and the raise queued for the next
        flush, and is logged; it never undoes the store's commit. Inside a seal-lock hold the durable part is refused
        (``RuntimeError``, F5) after the in-memory gate is up."""
        # round 4 (F-C1): the in-memory gate FIRST, before any check can fail (a plain attribute write, no lock: a
        # forked child must never block here), then validation, then the queue under the seal lock
        self._gate_up = True
        if self._forked():
            raise MembershipUnsafe("forked")
        if type(device) is not str or not (_HEX32.fullmatch(device) or device == ""):
            raise ValueError("gate device")
        if reason not in GATE_REASONS:
            raise ValueError("gate reason")
        with self._seal:
            self._gate_up = True
            self._gate_queue.append({"device": device, "reason": reason})
        try:
            self.flush_gate()
        except (StateError, OSError) as exc:
            log.error(
                "membership gate raise not yet durable (%s); it stays up in memory and queued", type(exc).__name__
            )

    def bind_gate(self, live: Callable[[], Any], hook: object) -> None:
        """Bind the gate to the live store (:meth:`R6Gate.install`): ``live()`` returns the store now in use (the
        host's ``service.store``), ``hook`` is the exact callable installed as its ``relay_gate``."""
        with self._seal:
            self._live, self._hook = live, hook

    def _hooked(self) -> bool:
        """Data may be sealed only while the store now in use carries our hook (a reopened, replaced or closed store
        does not: gated until :meth:`R6Gate.install` runs on it). Fail closed on any error."""
        if self._hook is None:
            return False
        try:
            store = self._live()
            return store is not None and store.relay_gate is self._hook
        except Exception:  # noqa: BLE001 - any doubt is "not hooked"
            return False

    def gate_up(self) -> bool:
        if self._forked():
            return True
        with self._seal:
            return self._gate_up

    def prune_stale_reasons(self, standing_now: Callable[[], Callable[[str], bool]]) -> int:
        """Round 5 to 7 (F-1): drop every durable ``f1_standing`` gate reason whose device (32 hex) is ``ok``, and lower
        the gate when no reason is left. A gate LOWERING, so atomic and monotone. **The caller MUST hold the live
        store's lock** across the call (store lock, then this writer lock; ``relayr6._rebuild_gate`` asserts it):

        * ``standing_now()`` is called INSIDE the writer lock, after the head the prune starts from is taken (round 7:
          a reload between an earlier snapshot and the prune could raise a fresh reason that the prune then judged
          against the stale snapshot); it returns a pure function of ONE store state snapshot read at that moment
          (``device_standing_ok(store.state, ...)``), never a call that can re-enter the store and reload;
        * compare-and-swap: if ``head.json`` changed or a raise is queued between that start and the write, nothing is
          written (``0``);
        * the durable write comes first; the in-memory gate goes down only after it, and only if the head is still the
          one just written and no raise is queued.

        Only ``f1_standing`` reasons (never ``held_evidence``, ``device.removed`` etc., ``h7`` or ``""``). Refused while
        unsafe or forked."""
        self._require_safe()
        with self._write():
            start = self._head
            gate = start["gate"]
            if gate is None:
                return 0
            standing_ok = standing_now()
            keep, dropped = [], 0
            for r in gate["reasons"]:
                if r["reason"] == "f1_standing" and r["device"] and standing_ok(r["device"]) is True:
                    dropped += 1
                else:
                    keep.append(r)
            if not dropped:
                return 0
            new_gate = {**gate, "reasons": keep} if keep else None
            new = {**start, "gate": new_gate}
            self._fault("before_prune_write")
            with self._seal:
                if self._head is not start or self._gate_queue:  # changed or queued meanwhile: never a stale copy
                    return 0
            self._dir.write_json(HEAD, new)
            self._head = new
            self._fault("after_prune")
            with self._seal:
                if new_gate is None and self._head is new and not self._gate_queue:
                    self._gate_up = False
            return dropped

    def flush_gate(self) -> bool:
        """R6's thread, before any publication work: write every queued gate reason into ``head.json`` (atomic).
        ``True`` when something was written. The in-memory gate stays up whatever happens here."""
        with self._write():
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
            self._head = new
            self._fault("after_gate")
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
        with self._write():
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

    @property
    def previously_linked(self) -> bool:
        """The workspace is (or may be) linked to a relay: its epochs then come from the relay path only. Fresh on
        every call: the construction-time answer OR the host's probe (a relay linked later in this process counts)."""
        return self._linked_now()

    def _linked_now(self) -> bool:
        if self._previously_linked:
            return True
        probe = self.link_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - an unreadable link state counts as linked (fail closed)
            return True

    def has_local_epochs(self) -> bool:
        """This store made an epoch without a relay (:meth:`advance_local`): see :data:`LOCAL_MARK`."""
        with self._write():
            return self._dir.read_json(LOCAL_MARK) is not None

    def _complete_exposed(self, ex: Mapping[str, Any]) -> bool:
        """Commit an exposed body left by a crash between ``expose`` and ``commit`` when it is complete and valid for
        this store (its own workspace, list_seq, epoch, digest and ``wk_commit`` of the claimed key); ``False``
        (nothing written) otherwise."""
        try:
            body = crypto.unb64u(ex["body"])
            o = _list_of(body)
            if (
                o.get("workspace_id") != self.workspace_id
                or o.get("epoch") != ex["epoch"]
                or o.get("list_seq") != ex["list_seq"]
                or ex["digest"] != _list_digest(body)
                or o.get("wk_commit") != self.wk_commit_of(ex["epoch"])
            ):
                return False
            return self.commit(
                list_seq=ex["list_seq"], epoch=ex["epoch"], list_hash=ex["digest"], list_obj=o, at_ms=self._now_ms()
            )
        except (CustodyRefused, MembershipUnsafe, crypto.EncodingError, KeyError, TypeError, ValueError):
            return False

    def advance_local(self, owner_person_id: str) -> int:
        """A relay-less workspace makes its own epoch (V3 C2b): the first one (the genesis epoch, an empty list) or the
        next one (the committed list's members carried over), through the same write-ahead steps as a rotation:
        ``new_epoch_key``, one ``expose``, one ``commit``. Same file formats, same custody, no new crypto; the list is
        not signed or sent anywhere (no relay). Refused, fail closed, for a workspace that is or was linked to a relay
        (checked fresh here, not at open): its epochs are the publisher's. The ``local-epochs.json`` mark is written
        before any key, so a store with local epochs is always marked (a relay link is refused for it).

        A crash between ``expose`` and ``commit`` leaves an exposed body: it is committed when complete and valid
        (that epoch is the one returned), else abandoned (its ``list_seq`` and epoch burned), then a new one is made."""
        self._require_safe()
        if self._linked_now():
            raise CustodyRefused("a relay-linked workspace gets its epochs from the relay path")
        with self._write():
            if self._dir.read_json(LOCAL_MARK) is None:
                self._dir.write_json(LOCAL_MARK, {"v": 1, "created_ms": self._now_ms()})
            ex = self._head["exposed"]
            if ex is not None:
                if self._complete_exposed(ex):
                    return int(ex["epoch"])
                self.abandon()
            with self._seal:
                ehw, lhw = self._ehw, self._lhw
            committed = self._head["committed"]
            members = committed["list"]["members"] if committed else []
            epoch = ehw + 1
            commit = self.new_epoch_key(epoch)
            try:
                o = memberlist.member_list_o(
                    self.workspace_id, epoch, lhw + 1, self._now_ms(), commit, members,
                    owner_person_id=owner_person_id, first=False,
                )  # fmt: skip
            except Refused:
                raise CustodyRefused("local member list") from None
            body = canon.dumps(o)
            digest = _list_digest(body)
            self.expose(list_seq=lhw + 1, epoch=epoch, body=body, digest=digest, adds=[])
            if not self.commit(list_seq=lhw + 1, epoch=epoch, list_hash=digest, list_obj=o, at_ms=self._now_ms()):
                raise CustodyRefused("the local epoch was not committed")
            return epoch

    def listed_devices(self) -> list[tuple[str, str]]:
        """``(device_id, person_id)`` of every member of the committed list and of the exposed body (a removed
        device may hold the exposed epoch's key once a late 2xx commits it, round 3 R4); a malformed entry is
        ``("", "")`` (the caller gates every device)."""
        with self._write():
            lists = [self._head["committed"]["list"]] if self._head["committed"] else []
            if self._head["exposed"] is not None:
                lists.append(_list_of(crypto.unb64u(self._head["exposed"]["body"])))
        out = []
        for o in lists:
            for m in o.get("members", []) if isinstance(o, dict) else [None]:
                ok = isinstance(m, dict) and type(m.get("device_id")) is str and type(m.get("person_id")) is str
                out.append((m["device_id"], m["person_id"]) if ok else ("", ""))
        return out

    def wk_commit_of(self, epoch: int) -> str | None:
        """``wk_commit`` of a held epoch (any), for R6b's planner; never the key."""
        with self._seal:
            wk = self._wk.get(epoch) if type(epoch) is int else None
        return None if wk is None else pairing.wk_commit(self.workspace_id, epoch, wk)

    def head(self) -> dict[str, Any]:
        """A deep copy of ``head.json`` (no secrets in it)."""
        with self._write():
            return json.loads(_doc(self._head))

    # -- R6c: membership/primary.json (11A.15.1, 11A.15.3: written by relay.primary_set only) -----------------------

    def read_primary(self) -> dict[str, Any] | None:
        """``primary.json`` as stored (``None``: none set). The caller verifies it under the genesis ``PK`` before
        every use (11A.15.1, Opus M7); an unreadable file raises (no primary: fail closed)."""
        with self._write():
            obj = self._dir.read_json(PRIMARY)
        if obj is not None and type(obj) is not dict:
            raise StateError("primary.json is not an object")
        return obj

    def write_primary(self, obj: Mapping[str, Any]) -> None:
        """``relay.primary_set``'s write (the host verified the certificate and cleaned the label first)."""
        with self._write():
            self._dir.write_json(PRIMARY, dict(obj))

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
        with self._write():
            with self._seal:
                ec, ehw, lhw = self._ec, self._ehw, self._lhw
                held = epoch in self._wk if type(epoch) is int else False
                burned_now = dict(self._burned)
            if not _int(list_seq, lhw + 1, MAX_SAFE_INT):
                raise CustodyRefused("list_seq at or below the high-water mark")
            rotation = epoch != ec
            if rotation and (not _int(epoch, ec + 1, MAX_EPOCH) or not held or epoch in burned_now):
                raise CustodyRefused("rotation epoch: above e_c, claimed, not burned")
            if rotation and epoch != ehw:
                raise CustodyRefused("rotation epoch below the high-water mark: only the newest claimed key")
            if not rotation and ec < 1:
                raise CustodyRefused("the first list is a rotation")
            o = _list_of(body)
            if digest != _list_digest(body):
                raise CustodyRefused("the digest is not the exposed list's")
            if (
                o.get("epoch") != epoch
                or o.get("list_seq") != list_seq
                or o.get("wk_commit") != self.wk_commit_of(epoch)
            ):
                raise CustodyRefused("the exposed body is not a list at this list_seq, epoch and key")
            head = self._head
            burned = {int(k): v for k, v in head["burned"]["epochs"].items()}
            seqs = list(head["burned"]["list_seqs"])
            old = head["exposed"]
            if old is not None:
                seqs.append(old["list_seq"])
                oe = old["new_epoch"]
                if oe is not None:
                    burned[oe] = epoch if rotation and epoch > oe else min(max(ehw, epoch) + 1, MAX_EPOCH)
            holders = {k: list(v) for k, v in head["holders"].items()}
            # H5: every device this body grants the epoch's key to is a holder of it from now on, accepted or not
            holders[str(epoch)] = sorted({*holders.get(str(epoch), []), *adds})
            new = {
                **head,
                "lhw": list_seq,
                "ehw": max(ehw, epoch),
                "holders": holders,
                "exposed": {"list_seq": list_seq, "epoch": epoch, "new_epoch": epoch if rotation else None,
                            "body": crypto.b64u(body), "digest": digest, "adds": sorted(set(adds)),
                            "first_send_ms": None, "attempts": 0},
                "burned": {"epochs": {str(e): n for e, n in sorted(burned.items())}, "list_seqs": sorted(set(seqs))},
            }  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._head = new
            self._fault("after_expose")
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
        with self._write():
            ex = self._head["exposed"]
            if ex is None or ex["list_seq"] != list_seq or ex["epoch"] != epoch:
                return False
            with self._seal:
                wk, ec, queued = self._wk.get(epoch), self._ec, bool(self._gate_queue)
            if wk is None:
                raise MembershipUnsafe("key_missing")
            commit = pairing.wk_commit(self.workspace_id, epoch, wk)
            exposed_o = _list_of(crypto.unb64u(ex["body"]))  # F3: the gate decision and the record come from the
            try:  # exposed body itself, never from the caller's object
                same = canon.dumps(dict(list_obj)) == canon.dumps(exposed_o)
            except (canon.JcsError, TypeError, ValueError):
                same = False
            if not same or list_hash != ex["digest"] or exposed_o.get("wk_commit") != commit:
                raise CustodyRefused("the committed list is not the exposed body's")
            ids = {m.get("device_id") for m in exposed_o["members"] if isinstance(m, Mapping)}
            gate = self._head["gate"]
            lower = (
                gate is not None and epoch > ec and not queued and not any(r["device"] in ids for r in gate["reasons"])
            )
            new = {
                **self._head,
                "committed": {"list_seq": list_seq, "epoch": epoch, "list_hash": list_hash, "wk_commit": commit,
                              "list": exposed_o, "at_ms": at_ms},
                "exposed": None,
                "gate": None if lower else gate,
            }  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._head = new
            self._fault("after_commit")
            with self._seal:
                self._ec, self._pending = epoch, None
                if lower and not self._gate_queue and self._head["gate"] is None:
                    self._gate_up = False
            return True

    def abandon(self, *, exclude: Mapping[str, str] | None = None) -> None:
        """Abandon the exposed body (H4: a content or divergence refusal): its ``list_seq`` is burned, and its epoch
        if it introduced one (named ``EHW + 1``). One atomic write, which also puts ``exclude`` (``{device: reason}``,
        the devices that body newly added; 11A.15.4 "the exclusion set") into ``excluded``."""
        exclude = dict(exclude or {})
        if any(not _HEX32.fullmatch(d) or type(r) is not str or not r for d, r in exclude.items()):
            raise CustodyRefused("exclude")
        with self._write():
            ex = self._head["exposed"]
            if ex is None:
                return
            with self._seal:
                ehw = self._ehw
            burned = {int(k): v for k, v in self._head["burned"]["epochs"].items()}
            if ex["new_epoch"] is not None:
                burned[ex["new_epoch"]] = min(ehw + 1, MAX_EPOCH)
            seqs = sorted({*self._head["burned"]["list_seqs"], ex["list_seq"]})
            excluded = dict(self._head["excluded"])
            for d, reason in exclude.items():
                excluded.setdefault(d, {"reason": reason[:60], "at_ms": self._now_ms()})
            new = {**self._head, "exposed": None, "excluded": excluded,
                   "burned": {"epochs": {str(e): n for e, n in sorted(burned.items())}, "list_seqs": seqs}}  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._head = new
            self._fault("after_abandon")
            with self._seal:
                self._burned, self._pending = burned, None

    def exclude(self, device: str, reason: str) -> None:
        """Put ``device`` into ``excluded`` (a pairing that ended after the allocation, 11A.15.4): durable relay
        ineligibility, one atomic write. It never leaves except by a fresh pairing (R6c's re-admission)."""
        if not _HEX32.fullmatch(device if type(device) is str else "") or type(reason) is not str or not reason:
            raise CustodyRefused("exclude")
        with self._write():
            if device in self._head["excluded"]:
                return
            new = {**self._head, "excluded": {**self._head["excluded"],
                                              device: {"reason": reason[:60], "at_ms": self._now_ms()}}}  # fmt: skip
            self._dir.write_json(HEAD, new)
            self._head = new
            self._fault("after_exclude")

    def set_h8(self, count: int, alarm: bool) -> None:
        """§11 H8 (11A.15.4): the consecutive refusals and whether the alarm is raised, in ``head.json``."""
        if not _int(count, 0, MAX_SAFE_INT) or type(alarm) is not bool:
            raise CustodyRefused("h8")
        with self._write():
            h8 = {"count": count, "alarm": alarm}
            if self._head["h8"] == h8:
                return
            new = {**self._head, "h8": h8}
            self._dir.write_json(HEAD, new)
            self._head = new

    def committed_record(self) -> dict[str, Any] | None:
        """A copy of ``head.json`` ``committed`` (``None``: none, or the state is unsafe). Lock-free: ``_head`` is
        replaced whole by every write, never mutated, so this is safe inside a seal span and on R2's threads."""
        if self._forked() or self._unsafe is not None:
            return None
        c = self._head["committed"]
        return None if c is None else json.loads(_doc(c))

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
        with self._write():
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
        self._head = new
        self._fault("after_card_head")

    def card_accepted(self, card_seq: int) -> None:
        """The relay accepted ``card_seq`` (R1/R5 after the first ``PUT card``, R6d after a rotation)."""
        with self._write():
            card = self._head["card"]
            if card["pending_card_seq"] != card_seq:
                raise CustodyRefused("not the pending card")
            new = {**self._head, "card": {**card, "card_seq": card_seq, "pending_card_seq": None}}
            self._dir.write_json(HEAD, new)
            self._head = new

    def card_key_wrap(self, card_seq: int, dk_kx_pub: bytes, device_id: str) -> bytes:
        """``CK_s`` sealed to one device (purpose ``card``, 11A.15.5; R6b's ``card_key_wraps``), for a card_seq with
        a claimed card; the raw ``CK`` never leaves this object."""
        with self._write():
            if not _int(card_seq, 1, MAX_EPOCH) or f"card-{card_seq}.json" not in self._cards.names():
                raise CustodyRefused("no card at this card_seq")
            ck = self._read_key("ck", card_seq, "ck")
        try:
            return memberlist.card_key_wrap(dk_kx_pub, device_id, self.workspace_id, card_seq, ck)
        except Refused as e:
            raise CustodyRefused(f"card key wrap: {e.code}") from None

    def sealed_wk(self, epoch: int, dk_kx_pub: bytes, device_id: str) -> bytes:
        """``WK_E`` sealed to one device (purpose ``wk``, 11A.15.4 step 3: the grants are built after the key is
        claimed and before the ``head.json`` write that exposes them); refused for a burned epoch or one never
        held. The raw key never leaves this object (R2's ``wk``/``refusal_key`` aside)."""
        if self._forked():
            raise MembershipUnsafe("forked")
        with self._seal:
            wk = self._wk.get(epoch) if type(epoch) is int and epoch not in self._burned else None
        if wk is None:
            raise CustodyRefused("no sealable key for this epoch")
        try:
            return memberlist.seal_wk(dk_kx_pub, device_id, self.workspace_id, epoch, wk)
        except Refused as e:
            raise CustodyRefused(f"wk seal: {e.code}") from None

    def view_key(self, epoch: int) -> bytes:
        """``K_view(epoch)`` for the hub's ``log_sync`` (hub contract 10.7): ``HKDF(WK_epoch, "", "orch/v2/hub-view|" ||
        hex(workspace_id) || "|" || dec(epoch))``, derived here so only the derived key leaves this object. Same rule as
        :meth:`sealed_wk`: refused for a burned epoch or one never held."""
        if self._forked():
            raise MembershipUnsafe("forked")
        with self._seal:
            wk = self._wk.get(epoch) if type(epoch) is int and epoch not in self._burned else None
        if wk is None or not 1 <= epoch < 1 << 32:
            raise CustodyRefused("no view key for this epoch")
        return crypto.hkdf(wk, b"", b"orch/v2/hub-view|" + self.workspace_id.encode() + b"|" + str(epoch).encode())


class R6Gate:
    """The store's hook (11A.15.2): ``raise_for(device, reason)`` inside the store's critical section that appends
    F1 ``device.removed``/``device.revoked`` or holds forwarded evidence, before the store lock is released. The
    in-memory gate goes up under the seal lock (a seal that began first finishes first), then the raise is written
    durably (writer lock) before it returns. It never takes the store lock. :meth:`install` sets ``Store.relay_gate``
    (which the host's journal never replaces; the store calls it on every loss of F1 standing, whatever its cause)
    and binds the key store to the live store, which is gated whenever that store does not carry this hook."""

    __slots__ = ("_keys",)

    def __init__(self, keys: MembershipKeys) -> None:
        self._keys = keys

    def raise_for(self, device: str, reason: str) -> None:
        self._keys.raise_gate(device, reason)

    def install(self, live: Callable[[], Any], *, rebuild: Callable[[], None] | None = None) -> None:
        """``live()`` is the store now in use (re-read at every seal decision): set its ``relay_gate`` to this gate
        (from now on no loss of standing is missed), run ``rebuild()`` (the start-up gate rebuild), and only then bind
        the key store to it. A ``rebuild`` that raises leaves the key store unbound, so gated (round 3, R3)."""
        store = live()
        if store is None:
            raise MembershipUnsafe("no store to hook")
        hook = self.raise_for
        store.relay_gate = hook
        if rebuild is not None:
            rebuild()
        self._keys.bind_gate(live, hook)
