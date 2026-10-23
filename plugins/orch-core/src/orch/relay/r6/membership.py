"""R6b: the membership publisher (11A.15.4, protocol §7.2/§7.3 and §19.2 ``PUT /v2/workspaces/{ws}/membership``).

:class:`MembershipPublisher` is the real :class:`orch.relay.r6.pair.MembershipQueue` (the seam
``orch.host.relaypair.MEMBERSHIP``) and the membership thread ``orch-relay-r6m-<id8>`` (11A.15.2 "Threads"). It
decides what to publish from three inputs, none of them the relay's: the devices of ``membership/devices/`` (R6c), the
host's F1 standing of each (a worker job under the store lock, supplied as ``standing``), and ``head.json``
(``committed``, ``holders``, ``excluded``, the gate).

**The plan** (:func:`plan_publication`): ``want`` = eligible devices minus ``excluded`` minus every device of a gate
reason. A *rotation* (fresh epoch and key) when a listed device left ``want``, the gate is up, or a holder of the
committed epoch is not wanted (the holder invariant, Codex 3); an *addition* (same epoch, the new grant only made) when
``want`` only grew; nothing when ``want`` equals the committed list. The first list and the empty rotation are
rotations.

**Write-ahead (H1/H2)**, in this order and only in the membership thread: ``flush_gate()``; the allocation
(``list_seq = LHW + 1``; for a rotation ``new_epoch_key(E)`` claims ``WK_E``); the list ``o`` through
``memberlist.member_list_o``, signed by ``RelaySigner.member_list`` (the H4 floor lives there); one grant per wanted
device (``sealed_wk``, never the raw key, signed by ``RelaySigner.wk_grant``, claimed write-once under ``grants/``
BEFORE the expose so a resend and a same-epoch addition use the exact bytes the signer ledger recorded); the card-key
wraps (``card_key_wrap``) for the devices new to the list once a card was accepted; one atomic ``expose``; only then
the first send. No writer method is called inside ``serialised()``.

**Outcomes**: 2xx with exactly ``{list_seq, epoch}`` of the body: ``commit`` (the exposed body only; a late 2xx for an
abandoned one is ignored) and ``on_committed`` for the offers it added. A transient answer (transport, 5xx, 429
``rate_limited``, a malformed 2xx) resends the exact bytes after a backoff and schedules a verified ``GET members``
(divergence, H7). A content or divergence refusal (the §7.2 list) is NEVER retried: ``abandon`` burns the ``list_seq``
and a new epoch, the devices that body added enter ``excluded`` in the same atomic write, ``on_refused`` ends their
offers, the next plan rotates (the holder invariant). 429 ``quota_exceeded`` is permanent: the same, alarm
``membership_quota``, and no new pairing (``ready()`` is false). A container-only refusal (``stale_card``,
``wrap_conflict``) is held with an alarm (follow-up: rebuild the wraps). A fence or session error ends the thread and
tells the worker (``on_error``).

**H7**: :meth:`MembershipPublisher.reconcile` reads ``GET members`` (verified under the WSK) and, when the relay holds a
list above ``LHW``/``EHW`` or another body at a ``list_seq`` this host exposed, calls ``recover_h7``.

Judgement calls and gaps are listed in 11A.15 "As built in B1".
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orch import canon, crypto
from orch.identity import memberlist
from orch.identity.epochs import CLAMP, FIRST_EPOCH_MAX, CustodyRefused, MembershipKeys, MembershipUnsafe
from orch.identity.errors import Refused
from orch.net.http import RedirectRefused, Transport, TransportError
from orch.wsproto.sign import RelaySigner, SignerRefused
from orch.wsproto.state import StateDir, StateError

from ..directory import Directory
from ..retry import RelayRefused, delay
from ..session import Fenced, LoginHeldOff, NotHolder, RelayMisbehaving, Session, SessionError
from .view import list_hash_of

__all__ = ["GrantStore", "MembershipPublisher", "Plan", "plan_publication"]

log = logging.getLogger("orch.relay.r6.membership")

BODY_CAP = 2 * 1024 * 1024 + 64 * 1024  # §19.2: the JSON cap is 2 MiB (a little room for the container)
ANSWER_CAP = 64 * 1024
SEND_DEADLINE_S = 30.0
IDLE_S = 5.0  # the thread re-judges the plan at least this often (a wake is immediate)
START_POLL_S = 0.5
H8_ALARM_AFTER = 3  # consecutive content refusals before the H8 alarm
ROTATION_STUCK_MS = 600_000  # §11 H8: an uncommitted rotation older than 10 minutes is alarmed
_HEX32 = re.compile(r"[0-9a-f]{32}")
#: container-only refusals (nothing is burned): the card number or a wrap, not the list
CONTAINER_REFUSALS = frozenset({"stale_card", "wrap_conflict"})
#: the §7.2/§7.3 content and divergence refusals of ``PUT membership`` (and the permanent quota): the body is abandoned.
#: Anything else (an auth answer after a re-login, an unknown code) is transient: the same bytes after a backoff.
ABANDON_CODES = frozenset({
    "malformed", "bad_signature", "other_workspace", "other_person", "bad_epoch", "stale_list", "rotation_required",
    "wk_commit_changed", "missing_grant", "grant_conflict", "revoked", "scope_exceeded", "not_eligible",
    "not_for_this_device", "cert_expired", "quota_exceeded",
})  # fmt: skip


@dataclass(frozen=True)
class Plan:
    """What to publish next: ``rotation`` (fresh epoch and key) or an addition at the committed epoch, for exactly
    ``devices``."""

    rotation: bool
    devices: tuple[str, ...]


def plan_publication(head: Mapping[str, Any], eligible: Sequence[str], ehw: int = 0) -> Plan | None:
    """The publication that moves the committed list to the wanted one, or ``None`` (pure; 11A.15.4). ``ehw`` is the
    epoch high-water mark: above the committed epoch (an abandoned or burned rotation) a change is a rotation, never
    an addition at ``e_c`` (H4: the signer refuses an epoch below the highest signed one)."""
    gate = head["gate"]
    gated = {r["device"] for r in gate["reasons"]} if gate else set()
    want = set(eligible) - set(head["excluded"]) - gated
    committed = head["committed"]
    if committed is None:
        return Plan(True, tuple(sorted(want))) if want else None
    cur = {m["device_id"] for m in committed["list"]["members"]}
    holders = set(head["holders"].get(str(committed["epoch"]), []))
    if (cur - want) or gate is not None or (holders - want):
        return Plan(True, tuple(sorted(want)))
    if want == cur:
        return None
    return Plan(ehw > committed["epoch"], tuple(sorted(want)))


class GrantStore:
    """``membership/grants/``: the exact signed ``wk_grant`` of a ``(device, epoch)``, claimed write-once before the
    ``head.json`` write that exposes a body containing it (11A.15.3). Flat: ``g<epoch>-<device>.json``."""

    def __init__(self, root: Any) -> None:
        self._d = StateDir(root)
        self._d.discard_temps()

    def close(self) -> None:
        self._d.close()

    @staticmethod
    def _name(epoch: int, device: str) -> str:
        if type(epoch) is not int or epoch < 1 or not _HEX32.fullmatch(device):
            raise StateError("grant name")
        return f"g{epoch}-{device}.json"

    def get(self, epoch: int, device: str) -> dict[str, Any] | None:
        raw = self._d.read(self._name(epoch, device))
        if raw is None:
            return None
        obj = canon.loads_strict(raw)
        if type(obj) is not dict:
            raise StateError("grant shape")
        return obj

    def get_unsigned(self, epoch: int, device: str) -> dict[str, Any] | None:
        """The unsigned grant object (sealed bytes included) persisted before signing (``u<epoch>-<device>.json``)."""
        raw = self._d.read("u" + self._name(epoch, device)[1:])
        if raw is None:
            return None
        obj = canon.loads_strict(raw)
        if type(obj) is not dict:
            raise StateError("grant shape")
        return obj

    def keep_unsigned(self, epoch: int, device: str, go: Mapping[str, Any]) -> dict[str, Any]:
        """Claim the unsigned grant write-once; the stored one wins (Ed25519 is deterministic, and the signer's ledger
        returns the recorded signature for exactly these bytes)."""
        name = "u" + self._name(epoch, device)[1:]
        if self._d.claim(name, canon.dumps(dict(go))):
            return dict(go)
        held = self.get_unsigned(epoch, device)
        assert held is not None
        return held

    def get_wrap(self, card_seq: int, device: str) -> str | None:
        raw = self._d.read(self._wrap_name(card_seq, device))
        if raw is None:
            return None
        obj = canon.loads_strict(raw)
        if type(obj) is not dict or type(obj.get("sealed")) is not str:
            raise StateError("wrap shape")
        return obj["sealed"]

    def keep_wrap(self, card_seq: int, device: str, sealed: str) -> str:
        """Claim the card-key wrap of ``(card_seq, device)`` write-once (§19.7: persisted before it is sent; different
        bytes for the pair are ``wrap_conflict``); the stored one wins."""
        if self._d.claim(self._wrap_name(card_seq, device), canon.dumps({"sealed": sealed})):
            return sealed
        held = self.get_wrap(card_seq, device)
        assert held is not None
        return held

    @staticmethod
    def _wrap_name(card_seq: int, device: str) -> str:
        if type(card_seq) is not int or card_seq < 1 or not _HEX32.fullmatch(device):
            raise StateError("wrap name")
        return f"w{card_seq}-{device}.json"

    def keep(self, epoch: int, device: str, signed: Mapping[str, Any]) -> dict[str, Any]:
        """Claim ``signed``; if another claim won (a crash after the claim), the stored one is returned."""
        name = self._name(epoch, device)
        if self._d.claim(name, canon.dumps(dict(signed))):
            return dict(signed)
        held = self.get(epoch, device)
        assert held is not None
        return held


class MembershipPublisher:
    """The membership queue (``bind``, ``ready``, ``post_addition``, ``post_exclusion``) and its thread. Every
    dependency is a callable or an object, so ``orch.relay`` stays free of ``orch.host``:

    * ``standing(device_ids) -> set[str]``: the eligible subset (F1 standing ok for the exact pair, no ``HostPins``
      evidence, certificate not expired), computed by the host as ONE worker job under the store lock;
    * ``device(id)``: ``devices/<id>.json`` (R6c);
    * ``on_error(exc)``: a fence or session error ended the thread."""

    def __init__(
        self,
        keys: MembershipKeys,
        signer: RelaySigner,
        *,
        session: Session,
        conn: Callable[[], Transport],
        grants: GrantStore,
        owner_pk_pub: bytes,
        wsk_pub: bytes,
        standing: Callable[[list[str]], set[str]],
        device: Callable[[str], Mapping[str, Any] | None],
        device_ids: Callable[[], list[str]],
        alarm: Callable[[str, str], None],
        on_error: Callable[[BaseException], None] = lambda e: None,
        now_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        rng: random.Random | None = None,
        id8: str | None = None,
    ) -> None:
        self._keys, self._signer, self._session, self._conn_factory = keys, signer, session, conn
        self._grants = grants
        self._owner_pk, self._wsk = owner_pk_pub, wsk_pub
        self._owner = crypto.person_id(owner_pk_pub).hex()
        self._standing, self._device, self._device_ids = standing, device, device_ids
        self._alarm_cb, self._on_error, self._now_ms = alarm, on_error, now_ms
        self._rng = rng or random.SystemRandom()
        self.ws = keys.workspace_id
        self.id8 = id8 or self.ws[:8]
        self._lock = threading.Lock()
        self._events: Any = None
        self._offers: dict[str, str] = {}  # offer_id -> device_id, posted and not yet answered
        self._exclusions: list[tuple[str, str]] = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn: Transport | None = None
        self._failures = 0
        self._not_before = 0.0
        self._reconcile_due = False
        self._quota_full = False
        self._first_send: dict[int, int] = {}  # list_seq -> first send ms (H8's 10-minute alarm)
        self._alarmed: set[str] = set()
        self._no_wraps: set[int] = set()  # list_seqs whose card_key_wraps the relay refused: sent without them
        self.error: BaseException | None = None

    # -- the MembershipQueue the pair thread sees ------------------------------------------------------------------

    def bind(self, events: Any) -> None:
        self._events = events

    def ready(self) -> bool:
        t = self._thread
        return (
            t is not None and t.is_alive() and not self._stop.is_set() and not self._quota_full
            and self._session.gen is not None
        )  # fmt: skip

    def post_addition(self, offer_id: str, device_id: str, cert: dict[str, Any]) -> None:
        """Idempotent per offer and device. Never blocks: queued, answered later through the events."""
        del cert  # the certificate and scopes are read from devices/<id>.json
        if not _HEX32.fullmatch(device_id if type(device_id) is str else "") or type(offer_id) is not str:
            return
        with self._lock:
            self._offers[offer_id] = device_id
        self._wake.set()

    def post_exclusion(self, offer_id: str, device_id: str, reason: str) -> None:
        with self._lock:
            self._offers.pop(offer_id, None)
            self._exclusions.append((device_id, str(reason)[:60] or "ended"))
        self._wake.set()

    # -- lifecycle (RelayWorker companion: start, stop, join) -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("started twice")
        self._thread = threading.Thread(target=self._run, name=f"orch-relay-r6m-{self.id8}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        c = self._conn
        if c is not None:
            try:
                c.shutdown()
            except Exception:  # noqa: BLE001 - best effort
                pass

    def join(self, timeout: float) -> bool:
        t = self._thread
        if t is None:
            return True
        t.join(max(0.0, timeout))
        return not t.is_alive()

    def wake(self) -> None:
        self._wake.set()

    # -- the thread -------------------------------------------------------------------------------------------------

    def _alarm(self, code: str, detail: str = "") -> None:
        if code in self._alarmed:
            return
        self._alarmed.add(code)
        self._alarm_cb(code, detail)

    def _transport(self) -> Transport:
        if self._conn is None:
            self._conn = self._conn_factory()
        return self._conn

    def _run(self) -> None:
        try:
            while not self._stop.is_set() and self._session.gen is None:
                self._stop.wait(START_POLL_S)  # nothing before this holder's own host/hold
            head = self._keys.head()
            self._reconcile_due = bool(head["gate"] and any(r["reason"] == "h7" for r in head["gate"]["reasons"]))
            while not self._stop.is_set():
                wait = IDLE_S
                try:
                    wait = self._step()
                except (Fenced, NotHolder) as e:
                    raise e
                except (LoginHeldOff, RelayMisbehaving) as e:
                    self._alarm("membership_relay_misbehaving", type(e).__name__)
                    wait = self._backoff()
                except (TransportError, RedirectRefused) as e:
                    log.info("relay membership: transport %s", e.kind if isinstance(e, TransportError) else "redirect")
                    wait = self._backoff()
                except SessionError as e:  # any other session error ends the thread, like a fence
                    raise e
                except (MembershipUnsafe, CustodyRefused, StateError, SignerRefused, Refused) as e:
                    self._alarm("membership_local", type(e).__name__)
                    wait = self._backoff()
                self._wake.wait(wait)
                self._wake.clear()
        except Exception as e:  # noqa: BLE001 - a fence or session error (or a bug): the thread stops, R5 is told
            if not self._stop.is_set():
                self.error = e
                log.error("relay membership thread stopped: %s", type(e).__name__)
                self._on_error(e)
            self._stop.set()

    def _backoff(self) -> float:
        self._failures += 1
        self._reconcile_due = True
        return delay(self._failures - 1, None, self._rng)

    # -- one step ---------------------------------------------------------------------------------------------------

    def _step(self) -> float:
        """One turn: returns how long to wait before the next (a wake ends the wait at once)."""
        keys = self._keys
        keys.check_start()
        self._drain_exclusions()
        keys.flush_gate()
        if self._reconcile_due:
            self.reconcile()
        head = keys.head()
        eligible = self._eligible()
        self._answer_known(head, eligible)
        plan = plan_publication(head, eligible, keys.high_water()[1])
        ex = head["exposed"]
        if ex is not None and (plan is None or not self._same(ex, plan)):
            keys.abandon()  # a change supersedes the exposed body (H4): its list_seq (and epoch) burn
            head = keys.head()
            ex = None
        if plan is None:
            return IDLE_S
        if ex is None:
            ex = self._expose(plan, head)
            if ex is None:
                return IDLE_S
        return self._send(ex, head)

    def _drain_exclusions(self) -> None:
        with self._lock:
            todo, self._exclusions = self._exclusions, []
        for device, reason in todo:
            if _HEX32.fullmatch(device):
                self._keys.exclude(device, reason)

    def _eligible(self) -> list[str]:
        ids = self._local(self._device_ids)
        ok = self._local(self._standing, ids) if ids else set()
        return sorted(d for d in ids if d in ok)

    @staticmethod
    def _local(fn: Callable[..., Any], *args: Any) -> Any:
        """A host-supplied input (device store, standing job) that fails is a local fault: ``membership_local`` and a
        backoff, never the end of the worker. Session, fence and transport errors pass through."""
        try:
            return fn(*args)
        except (SessionError, TransportError, RedirectRefused):
            raise
        except Exception as e:  # noqa: BLE001 - PairStoreError, HostError, a failed worker job
            raise StateError(f"host input failed: {type(e).__name__}") from None

    def _answer_known(self, head: Mapping[str, Any], eligible: Sequence[str]) -> None:
        """An offer whose device is already in the committed list is answered at once (a repost after a crash); one
        whose device can never be published (excluded, not eligible) is refused."""
        committed = head["committed"]
        with self._lock:
            offers = dict(self._offers)
        listed = {m["device_id"] for m in committed["list"]["members"]} if committed else set()
        for offer, device in offers.items():
            if committed is not None and device in listed:
                self._tell_committed(offer, device, committed)
            elif device in head["excluded"]:
                self._refuse(offer, "excluded")
            elif device not in eligible and not self._pending_device(device):
                self._refuse(offer, "device_not_eligible")

    def _pending_device(self, device: str) -> bool:
        """The device record does not exist yet (the allocation commits it before the post, so this is rare)."""
        return self._local(self._device, device) is None

    def _tell_committed(self, offer: str, device: str, committed: Mapping[str, Any]) -> None:
        with self._lock:
            if self._offers.pop(offer, None) is None:
                return
        pub = {"list_seq": committed["list_seq"], "epoch": committed["epoch"], "adds": [device],
               "list_hash": committed["list_hash"]}  # fmt: skip
        if self._events is not None:
            self._events.on_committed(offer, pub)

    def _refuse(self, offer: str, reason: str) -> None:
        with self._lock:
            if self._offers.pop(offer, None) is None:
                return
        if self._events is not None:
            self._events.on_refused(offer, reason)

    @staticmethod
    def _same(ex: Mapping[str, Any], plan: Plan) -> bool:
        body = canon.loads_strict(crypto.unb64u(ex["body"]))
        listed = tuple(sorted(m["device_id"] for m in body["member_list"]["o"]["members"]))
        return listed == plan.devices and (ex["new_epoch"] is not None) == plan.rotation

    # -- build and expose (11A.15.4 write-ahead) ----------------------------------------------------------------------

    def _expose(self, plan: Plan, head: Mapping[str, Any]) -> dict[str, Any] | None:
        keys, ws = self._keys, self.ws
        lhw, ehw = keys.high_water()
        committed = head["committed"]
        ec = committed["epoch"] if committed else 0
        list_seq = lhw + 1
        if plan.rotation:
            epoch = ehw + 1
            limit = ec + CLAMP if ec else (FIRST_EPOCH_MAX if lhw == 0 else 2**32 - 1)
            if epoch > limit:
                self._alarm("membership_epoch_clamp", "no epoch left below the clamp: publication frozen")
                return None
            commit = keys.new_epoch_key(epoch)
        else:
            assert committed is not None
            epoch, commit = ec, committed["wk_commit"]
        recs = {d: self._local(self._device, d) for d in plan.devices}
        entries = []
        for d, rec in recs.items():
            if not isinstance(rec, Mapping) or rec.get("person_id") != self._owner:
                self._alarm("membership_device_record", d[:8])
                return None
            entries.append({"device_id": d, "person_id": rec["person_id"], "scopes": list(rec["scopes"])})
        o = memberlist.member_list_o(
            ws, epoch, list_seq, self._now_ms(), commit, entries, owner_person_id=self._owner, first=list_seq == 1
        )
        signed = {"o": o, "sig": self._signer.member_list(o)}
        grants = [self._grant(epoch, d, recs[d], o) for d in plan.devices]
        body_obj: dict[str, Any] = {"member_list": signed, "grants": grants}
        listed = {m["device_id"] for m in committed["list"]["members"]} if committed else set()
        wraps = self._wraps(head, [d for d in plan.devices if d not in listed], recs)
        if wraps is not None:
            body_obj["card_key_wraps"] = wraps
        body = canon.dumps(body_obj)
        if len(body) > BODY_CAP:
            self._alarm("membership_too_large", f"{len(body)} bytes")
            return None
        keys.expose(list_seq=list_seq, epoch=epoch, body=body, digest=list_hash_of(signed), adds=plan.devices)
        return keys.head()["exposed"]

    def _grant(self, epoch: int, device: str, rec: Mapping[str, Any], o: Mapping[str, Any]) -> dict[str, Any]:
        held = self._grants.get(epoch, device)
        if held is not None:
            return held  # reused byte for byte: the signer ledger and the relay both compare the exact grant
        go = self._grants.get_unsigned(epoch, device)
        if go is None:  # sealed once, persisted BEFORE signing: a crash after the signature re-signs these bytes
            kx = crypto.unb64u(rec["cert"]["o"]["dk_kx_pub"], crypto.PUB_LEN)
            sealed = self._keys.sealed_wk(epoch, kx, device)
            go = self._grants.keep_unsigned(epoch, device, memberlist.wk_grant_o(self.ws, device, epoch, sealed))
        signed = {"o": go, "sig": self._signer.wk_grant(go, signed_list=o)}
        return self._grants.keep(epoch, device, signed)

    def _wraps(self, head: Mapping[str, Any], new: Sequence[str], recs: Mapping[str, Any]) -> dict[str, Any] | None:
        seq = head["card"]["card_seq"]
        if not new or type(seq) is not int or seq < 1:
            return None
        out = []
        for d in new:
            kx = crypto.unb64u(recs[d]["cert"]["o"]["dk_kx_pub"], crypto.PUB_LEN)
            sealed = self._grants.get_wrap(seq, d)
            if sealed is None:
                sealed = self._grants.keep_wrap(seq, d, crypto.b64u(self._keys.card_key_wrap(seq, kx, d)))
            out.append({"device_id": d, "sealed": sealed})
        return {"card_seq": seq, "wraps": out}

    # -- send and outcomes (H3/H4) -----------------------------------------------------------------------------------

    def _send(self, ex: Mapping[str, Any], head: Mapping[str, Any]) -> float:
        body = crypto.unb64u(ex["body"])
        seq, epoch = ex["list_seq"], ex["epoch"]
        if seq in self._no_wraps:
            body = self._without_wraps(body)
        now = self._now_ms()
        first = self._first_send.setdefault(seq, now)
        if ex["new_epoch"] is not None and now - first > ROTATION_STUCK_MS:
            self._alarm("membership_rotation_stuck", f"list_seq {seq} is not committed after 10 minutes")
        try:
            obj = self._session.call(
                "PUT", f"/v2/workspaces/{self.ws}/membership", "PUT /v2/workspaces/{ws}/membership", raw=body,
                content_type="application/json", holder=True, request_cap=BODY_CAP, cap=ANSWER_CAP,
                deadline_s=SEND_DEADLINE_S, keys={"list_seq", "epoch"}, transport=self._transport(),
            )  # fmt: skip
        except RelayRefused as e:
            return self._refused(e, ex, head)
        if obj["list_seq"] != seq or obj["epoch"] != epoch:
            raise RelayMisbehaving("PUT membership answered another list_seq or epoch")
        return self._committed(ex, head)

    def _committed(self, ex: Mapping[str, Any], head: Mapping[str, Any]) -> float:
        keys = self._keys
        shell = canon.loads_strict(crypto.unb64u(ex["body"]))
        o = shell["member_list"]["o"]
        ok = keys.commit(list_seq=ex["list_seq"], epoch=ex["epoch"], list_hash=ex["digest"], list_obj=o,
                         at_ms=self._now_ms())  # fmt: skip
        self._failures = 0
        self._first_send.pop(ex["list_seq"], None)
        keys.set_h8(0, False)
        if not ok:
            return 0.0  # a late 2xx for a body already abandoned: ignored, never activated
        committed = keys.committed_record()
        assert committed is not None
        before = {m["device_id"] for m in head["committed"]["list"]["members"]} if head["committed"] else set()
        added = sorted({m["device_id"] for m in o["members"]} - before)
        with self._lock:
            offers = dict(self._offers)
        for offer, device in offers.items():
            if device in added:
                self._tell_committed(offer, device, committed)
        return 0.0  # replan at once: more may be waiting

    def _refused(self, e: RelayRefused, ex: Mapping[str, Any], head: Mapping[str, Any]) -> float:
        permanent_quota = e.status == 429 and e.code == "quota_exceeded"
        if (e.status >= 500 or (e.status == 429 and e.code == "rate_limited")) and not permanent_quota:
            self._reconcile_due = True
            self._failures += 1
            return delay(self._failures - 1, e.retry_after, self._rng)
        if e.code in CONTAINER_REFUSALS:
            self._alarm("membership_card_refused", f"{e.status} {e.code}")
            if ex["list_seq"] not in self._no_wraps and b"card_key_wraps" in crypto.unb64u(ex["body"]):
                self._no_wraps.add(ex["list_seq"])  # same list and grants, no wraps: never the same refused bytes again
                return 0.0
            self._failures += 1
            return delay(self._failures - 1, e.retry_after, self._rng)
        if e.status == 404:  # an unknown route or workspace is not a verdict on the list
            self._alarm("membership_refused_404", "")
            self._failures += 1
            return delay(self._failures - 1, e.retry_after, self._rng)
        if e.code not in ABANDON_CODES:  # unauthenticated, expired session after a re-login, an unknown answer
            self._alarm("membership_refused_other", f"{e.status} {e.code}")
            self._reconcile_due = True
            self._failures += 1
            return delay(self._failures - 1, e.retry_after, self._rng)
        return self._content_refusal(e, ex, head)

    @staticmethod
    def _without_wraps(body: bytes) -> bytes:
        obj = canon.loads_strict(body)
        obj.pop("card_key_wraps", None)
        return canon.dumps(obj)

    def _content_refusal(self, e: RelayRefused, ex: Mapping[str, Any], head: Mapping[str, Any]) -> float:
        """H4: burn the list_seq (and the epoch it introduced) and never retry the body; the devices it newly added
        enter ``excluded`` in the same write and their offers end ``failed``; the next plan rotates."""
        keys = self._keys
        shell = canon.loads_strict(crypto.unb64u(ex["body"]))
        before = {m["device_id"] for m in head["committed"]["list"]["members"]} if head["committed"] else set()
        added = sorted({m["device_id"] for m in shell["member_list"]["o"]["members"]} - before)
        reason = str(e.code or e.status)
        keys.abandon(exclude={d: reason for d in added})
        self._first_send.pop(ex["list_seq"], None)
        self._reconcile_due = True
        with self._lock:
            offers = dict(self._offers)
        for offer, device in offers.items():
            if device in added:
                self._refuse(offer, reason)
        if e.status == 429 and e.code == "quota_exceeded":
            self._quota_full = True
            self._alarm("membership_quota", "the relay's ever-device quota is full: no new pairings")
        count = int(head["h8"].get("count", 0)) + 1
        keys.set_h8(count, count >= H8_ALARM_AFTER)
        if count >= H8_ALARM_AFTER:
            self._alarm("membership_refusals", f"{count} publications refused in a row")
        self._failures += 1
        return delay(count - 1, e.retry_after, self._rng)

    # -- H7: a verified GET members ----------------------------------------------------------------------------------

    def reconcile(self) -> None:
        """Read the relay's list (verified under the WSK) and apply H7 when it is above what this host allocated or is
        another body at a ``list_seq`` this host exposed. The relay's list is never adopted."""
        d = Directory(self._session, owner_pk_pub=self._owner_pk, wsk_pub=self._wsk, transport=self._transport())
        served = d.get_members()
        head = self._keys.head()
        lhw, ehw = self._keys.high_water()
        if served is None:
            self._reconcile_due = False
            return
        o = served["o"]
        r_seq, r_epoch = o["list_seq"], o["epoch"]
        ours: dict[int, str] = {}
        if head["committed"]:
            ours[head["committed"]["list_seq"]] = head["committed"]["list_hash"]
        if head["exposed"]:
            ours[head["exposed"]["list_seq"]] = head["exposed"]["digest"]
        diverged = r_seq > lhw or r_epoch > ehw or (r_seq in ours and ours[r_seq] != list_hash_of(served))
        if diverged:
            self._alarm("membership_h7", "the relay holds a list this host did not allocate: host-state loss")
            self._keys.recover_h7(relay_lhw=r_seq, relay_ehw=r_epoch)
        self._reconcile_due = False
