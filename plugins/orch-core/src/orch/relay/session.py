"""R1: the workspace session and the holder generation (contract 11A.5 "Session and holder", 11A.2 "Origin").

* **Login.** ``POST session/challenge``, then ``POST session/login {challenge, kind:"workspace", id, sig}`` with the
  signature from :meth:`RelaySigner.login`. Every login draws a fresh challenge; a challenge is never signed twice in
  this process (a relay that serves one again is misbehaving), and a login body is never resent.
* **Token.** In memory only, never logged, never in ``repr``. Validated as a header-safe token before use.
* **Re-login** is single-flight (one lock per session): before ``15 min - 120 s`` measured on the monotonic clock from
  the start of the login (the relay's ``expires_ms`` is never trusted, 11A.8), or once on a 401 ``unauthenticated`` /
  ``expired``, after which the identical request bytes are sent again once.
* **bad_signature.** A 401 ``bad_signature`` on login is not retried within 60 s (:class:`LoginHeldOff`), and after 3
  in a row the alarm ``origin_mismatch_suspected`` names the stored origin and :class:`OriginMismatch` stops the
  worker (a wrong stored origin only ever shows as this).
* **Relay info (H12d-3, orch-v2-host §0.8.6 item 9.8).** :func:`fetch_relay_info` reads ``GET /v2/relay-info``
  (no session, no workspace data): ``{"v":1, "reader":N, "max_publication_bytes"?}``; an absent route (404) is level
  0. Link step 1 calls it before the reservation; with the ``relay_info`` hook, every successful login reads it again
  and hands the level to the hook (the host stores it and alarms ``relay_reader_old``). A failed read at login keeps
  the last stored level and never fails the login. An honest-operator statement, never a security control.
* **Holder.** ``POST host/hold {gen: holder.gen or null, take_over}``; 409 ``holder_conflict`` waits out the lease
  (40 s, monotonic) and retries; ``fenced`` (on hold or on any W(g) write) alarms ``fenced``, and every later write is
  refused locally (:class:`Fenced`). Takeover (``take_over: true``) is only the explicit operator command.

Network retries of transient failures are the caller's (:class:`orch.relay.retry.Retrier`), never this module's.
"""

from __future__ import annotations

import collections
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from orch import crypto
from orch.net.http import Transport
from orch.wsproto import wire
from orch.wsproto.sign import RelaySigner
from orch.wsproto.state import HolderRecord, HolderStore, StateError

from .retry import RelayRefused

__all__ = [
    "Fenced",
    "Hold",
    "HolderStore",
    "LoginHeldOff",
    "NotHolder",
    "OriginMismatch",
    "RelayMisbehaving",
    "RelayInfo",
    "Session",
    "SessionError",
    "TakeoverRefused",
    "fetch_relay_info",
]

log = logging.getLogger("orch.relay.session")

TOKEN_LIFETIME_S = 900.0
RELOGIN_MARGIN_S = 120.0
LEASE_S = 40.0
BAD_SIGNATURE_HOLDOFF_S = 60.0
ORIGIN_ALARM_AFTER = 3
SMALL_CAP = 16 * 1024
SMALL_DEADLINE_S = 30.0
AUX_HOLD_DEADLINE_S = 10.0
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,512}")
_SEEN_CHALLENGES = 64


@dataclass(frozen=True)
class RelayInfo:
    """``GET /v2/relay-info`` (§0.8.6 item 9.8): the relay software's reader level (0 for an absent route) and its
    optional publication cap."""

    reader: int
    max_publication_bytes: int | None = None


RELAY_INFO_ROUTE = "GET /v2/relay-info"
_READER_MAX = 2**31 - 1


def fetch_relay_info(conn: Transport, *, deadline_s: float = SMALL_DEADLINE_S) -> RelayInfo:
    """Read ``GET /v2/relay-info`` once (no session). 404: level 0 (a relay before H12d). Any other non-2xx answer
    raises :class:`~orch.relay.retry.RelayRefused`; a 2xx body that is not exactly ``{"v":1, "reader":uint31}`` plus
    an optional positive ``max_publication_bytes`` raises :class:`RelayMisbehaving` (never a level)."""
    r = conn.request(
        "GET", "/v2/relay-info", route=RELAY_INFO_ROUTE, headers={}, body=None, deadline_s=deadline_s, cap=SMALL_CAP
    )
    if r.status == 404:
        return RelayInfo(0, None)
    if not 200 <= r.status < 300:
        raise RelayRefused.from_response(RELAY_INFO_ROUTE, r)
    try:
        obj = wire.loads_body(r.body, cap=SMALL_CAP)
    except wire.WireError:
        raise RelayMisbehaving("relay-info: malformed body") from None
    if type(obj) is not dict or not {"v", "reader"} <= set(obj) <= {"v", "reader", "max_publication_bytes"}:
        raise RelayMisbehaving("relay-info: field set")
    reader, cap = obj["reader"], obj.get("max_publication_bytes")
    if type(obj["v"]) is not int or obj["v"] != 1 or type(reader) is not int or not 0 <= reader <= _READER_MAX:
        raise RelayMisbehaving("relay-info: v or reader")
    if "max_publication_bytes" in obj and (type(cap) is not int or not 0 < cap <= wire.MAX_SAFE_INT):
        raise RelayMisbehaving("relay-info: max_publication_bytes")
    return RelayInfo(reader, cap)


class SessionError(Exception):
    """Base of the session's own refusals. Messages never carry a token, challenge or signature."""


class Fenced(SessionError):
    """The relay answered ``fenced``: another holder generation owns the workspace. The worker stops writing."""


class NotHolder(SessionError):
    """A W(g) write was attempted without a holder generation."""


class LoginHeldOff(SessionError):
    """A login within 60 s of a 401 ``bad_signature``."""


class OriginMismatch(SessionError):
    """Three 401 ``bad_signature`` logins in a row: the stored origin is probably not the relay's."""


class TakeoverRefused(Fenced):
    """The restart takeover's conditions failed (alarm ``relay_takeover_refused``); stopped until an operator
    takeover."""


class _Refusal(Exception):  # noqa: N818 - internal: a preflight refusal recorded under the lock, alarmed after it
    def __init__(self, why: str) -> None:
        super().__init__(why)
        self.why = why


class RelayMisbehaving(SessionError):
    """A success answer that is malformed or contradicts what was sent (alarm ``relay_misbehaving``)."""


@dataclass(frozen=True)
class Hold:
    gen: int
    lease_s: int
    cursor: int


def _int(v: object, low: int = 0) -> int:
    if type(v) is not int or not low <= v <= wire.MAX_SAFE_INT:
        raise RelayMisbehaving("integer field")
    return v


class Session:
    """One workspace session against one relay origin. ONE Session per state directory, shared by the workspace's relay
    threads (poll, resp, pub, aux): the login lock, the preflight pass and the sticky refusal are fields of this object,
    so a second Session on the same directory would not see this one's refusal (round 8, R7-4; 11A.5)."""

    def __init__(
        self,
        transport: Transport,
        signer: RelaySigner,
        *,
        holder: HolderStore,
        alarm: Callable[[str, str], None],
        mono: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop: threading.Event | None = None,
        relay_info: Callable[[int], None] | None = None,
    ) -> None:
        self._t, self._signer, self._holder, self._alarm = transport, signer, holder, alarm
        #: H12d-3: called with the relay's reader level after every successful login (R1, §0.8.6 item 9.8)
        self._relay_info = relay_info
        self._mono, self._sleep, self._stop = mono, sleep, stop
        self._login_lock = threading.Lock()
        # serialises preflight() (round 9): a passing run cannot set _checked after another thread's StateError. A leaf
        # lock: preflight does no network I/O and takes no other Session lock, and raises its alarm only after
        # releasing it (round 10). Reentrant so a same-thread re-entry (a signal handler, or a holder store calling
        # back into this Session) cannot deadlock it
        self._preflight_lock = threading.RLock()
        self._token: str | None = None
        self._relogin_at = 0.0
        self._bad_sig_at: float | None = None
        self._bad_sig_count = 0
        self._seen: collections.deque[bytes] = collections.deque(maxlen=_SEEN_CHALLENGES)
        self._gen: int | None = None
        self._fenced = False
        self._checked = False  # the local preflight passed (11A.5; gates every network call, round 6 #1)
        self._refused: str | None = None  # a preflight or takeover refusal: every later call is refused locally

    def __repr__(self) -> str:
        return f"Session(workspace={self.workspace_id}, logged_in={self._token is not None}, gen={self._gen})"

    @property
    def workspace_id(self) -> str:
        return self._signer.link.workspace_id

    @property
    def origin(self) -> str:
        return self._signer.link.origin

    @property
    def gen(self) -> int | None:
        return self._gen

    @property
    def fenced(self) -> bool:
        return self._fenced

    @property
    def logged_in(self) -> bool:
        return self._token is not None

    # -- the request primitive ------------------------------------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        route: str,
        *,
        body: Any = None,
        auth: bool = True,
        holder: bool = False,
        cap: int = SMALL_CAP,
        request_cap: int = SMALL_CAP,
        deadline_s: float = SMALL_DEADLINE_S,
        keys: frozenset[str] | set[str] | None = None,
        fenced_stops: bool = True,
        raw: bytes | None = None,
        transport: Transport | None = None,
        content_type: str = "application/octet-stream",
        resend_ok: bool = True,
    ) -> dict[str, Any]:
        """One relay call. ``body`` is built into ``cj`` bytes once; a 401 ``unauthenticated``/``expired`` re-logs in
        once and resends the identical bytes. 2xx bodies must be an object with exactly ``keys``; anything else raises
        :class:`RelayRefused` (or :class:`Fenced` for 409 ``fenced``).

        R2: ``raw`` sends those exact bytes as ``application/octet-stream`` (an ORB2 chunk; at most ``request_cap``),
        instead of a JSON ``body`` (R3: ``content_type="application/json"`` for a ``cj`` body built and counted once,
        a staging page or manifest). ``transport`` is the calling thread's own connection (11A.7: one ``RelayConn``
        per thread) while the login, the holder generation and the preflight stay this Session's, shared by the
        workspace's relay threads (round 8, R7-4)."""
        if raw is not None and (body is not None or type(raw) is not bytes or len(raw) > request_cap):
            raise wire.WireError("too_large" if type(raw) is bytes else "malformed", "raw request body")
        if content_type not in ("application/octet-stream", "application/json"):
            raise wire.WireError("malformed", "content type")
        if holder and self._fenced:
            raise Fenced("fenced: writes stopped")
        if not self._checked or self._refused is not None:
            # No request of any kind leaves before the local checks pass (round 6, orch-52 #1), and a refusal sticks
            # even after an earlier pass (round 7, R6-1): preflight() re-raises it; only operator_takeover clears it.
            self.preflight()
        data = raw if raw is not None else None if body is None else wire.dumps_body(body, cap=request_cap)
        conn = self._t if transport is None else transport
        resent = False
        while True:
            headers: dict[str, str] = {}
            if data is not None:
                headers["Content-Type"] = content_type if raw is not None else "application/json"
            token = None
            if auth:
                token = self._ensure_login()
                headers["Authorization"] = f"Bearer {token}"
            if holder:
                if self._gen is None:
                    raise NotHolder("no holder generation")
                headers["Orch-Holder"] = str(self._gen)
            extra = {} if resend_ok else {"resend_ok": False}  # the transport never resends this one
            r = conn.request(
                method, path, route=route, headers=headers, body=data, deadline_s=deadline_s, cap=cap, **extra
            )
            if 200 <= r.status < 300:
                try:
                    obj = wire.loads_body(r.body, cap=cap)
                except wire.WireError:
                    raise RelayMisbehaving(f"{route}: malformed success body") from None
                if type(obj) is not dict or (keys is not None and set(obj) != set(keys)):
                    raise RelayMisbehaving(f"{route}: success field set")
                return obj
            err = RelayRefused.from_response(route, r)
            log.info("relay %s refused status=%d code=%s", route, err.status, err.code)
            if auth and not resent and err.status == 401 and err.code in ("unauthenticated", "expired"):
                resent = True
                self._relogin(token)
                continue
            if err.status == 409 and err.code == "fenced" and fenced_stops:
                self._set_fenced(route)
                raise Fenced("fenced") from err
            raise err

    def _set_fenced(self, route: str) -> None:
        if not self._fenced:
            self._fenced = True
            self._alarm("fenced", f"{route} gen={self._gen}")

    # -- login ----------------------------------------------------------------------------------------------------

    def _ensure_login(self) -> str:
        token = self._token
        if token is not None and self._mono() < self._relogin_at:
            return token
        with self._login_lock:
            if self._token is None or self._mono() >= self._relogin_at:
                self._login()
            assert self._token is not None
            return self._token

    def _relogin(self, stale: str | None) -> None:
        with self._login_lock:
            if self._token is None or self._token == stale:
                self._token = None
                self._login()

    def login(self) -> None:
        """Log in now (single-flight)."""
        with self._login_lock:
            self._login()

    def challenge(self) -> bytes:
        obj = self.call(
            "POST", "/v2/session/challenge", "POST /v2/session/challenge", body={}, auth=False,
            keys={"challenge", "expires_ms"}, resend_ok=False,
        )  # fmt: skip
        try:
            ch = wire.b64_field(obj["challenge"], max_len=wire.CHALLENGE_LEN, exact=wire.CHALLENGE_LEN)
        except wire.WireError:
            raise RelayMisbehaving("challenge") from None
        _int(obj["expires_ms"])  # shape only: relay time is never trusted
        if any(wire.ct_equal(ch, s) for s in self._seen):
            raise RelayMisbehaving("the relay served a challenge this host already signed")
        self._seen.append(ch)
        return ch

    def _login(self) -> None:
        now = self._mono()
        if self._bad_sig_at is not None and now - self._bad_sig_at < BAD_SIGNATURE_HOLDOFF_S:
            raise LoginHeldOff("login held off after bad_signature")
        t0 = now
        ch = self.challenge()
        sig = self._signer.login(ch)
        body = {"challenge": crypto.b64u(ch), "kind": "workspace", "id": self.workspace_id, "sig": crypto.b64u(sig)}
        try:
            obj = self.call(
                "POST", "/v2/session/login", "POST /v2/session/login", body=body, auth=False,
                keys={"token", "expires_ms"}, resend_ok=False,
            )  # fmt: skip
        except RelayRefused as e:
            if e.status == 401 and e.code == "bad_signature":
                self._bad_sig_at = self._mono()
                self._bad_sig_count += 1
                if self._bad_sig_count >= ORIGIN_ALARM_AFTER:
                    self._alarm("origin_mismatch_suspected", f"stored origin {self.origin}")
                    raise OriginMismatch(
                        f"bad_signature {self._bad_sig_count} times; stored origin {self.origin}"
                    ) from e
            raise
        token = obj["token"]
        if type(token) is not str or not _TOKEN.fullmatch(token):
            raise RelayMisbehaving("token")
        _int(obj["expires_ms"])
        self._token = token
        self._relogin_at = t0 + TOKEN_LIFETIME_S - RELOGIN_MARGIN_S
        self._bad_sig_count = 0
        self._bad_sig_at = None
        log.info("relay login ok workspace=%s", self.workspace_id)
        if self._relay_info is not None:
            self._read_relay_info()

    def _read_relay_info(self) -> None:
        """The relay's reader level at this login (9.8), handed to the hook. Any failure keeps the last stored level
        (logged by class only); the login stands. The hook itself takes no store lock (it may run under the login
        lock on any relay thread)."""
        try:
            info = fetch_relay_info(self._t)
        except Exception as exc:  # noqa: BLE001 - an operator statement: a failed read changes nothing now
            log.info("relay-info not read at login (%s); the last level stays", type(exc).__name__)
            return
        assert self._relay_info is not None
        try:
            self._relay_info(info.reader)
        except Exception as exc:  # noqa: BLE001 - #555 review F5: the hook never fails the login
            log.error("relay-info hook failed at login (%s); the last level stays", type(exc).__name__)

    def logout(self, deadline_s: float = 5.0) -> None:
        """Best effort; the token is forgotten whatever the answer. After a refusal nothing is sent, not even the logout
        (round 7, R6-1: a refused session reaches the relay again only through :meth:`operator_takeover`)."""
        token, self._token = self._token, None
        if token is None or self._refused is not None:
            return
        try:
            self._t.request(
                "POST", "/v2/session/logout", route="POST /v2/session/logout",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                body=b"{}", deadline_s=deadline_s, cap=SMALL_CAP,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 - best effort by contract (11A.7 shutdown)
            log.info("relay logout failed (ignored)")

    # -- holder ---------------------------------------------------------------------------------------------------

    def hold(self, *, deadline_s: float = SMALL_DEADLINE_S) -> Hold:
        """Acquire (at start) or renew (while running) the holder generation (``host/hold``).

        While running, a renewal answered ``fenced`` stops the worker (alarm ``fenced``). At start, the restart rule
        decided by orch-52 (13 Oct 2026) applies, fail closed, never stealing a live lease:

        Before any network call, by :meth:`preflight` (order changed 13 Oct 2026, review R1 approved by orch-52; the
        same preflight gates every other entry point since round 6):

        1. The instance record must be bound to this very directory (its inode; ``dev`` is informational since it can
           change across a reboot): a copied or restored directory alarms ``relay_takeover_refused`` and stops.
        2. ``pending`` in ``holder.json`` (a takeover probe was in flight when we stopped): alarm ("possibly our own
           interrupted takeover") and stop.
        3. (a) ``holder.json`` says this state directory was the last holder and (b) its instance id is this
           directory's. Either failing: alarm and stop. A copied or restored directory (``cp -r``, rsync, a backup
           restore that recreates the directory) therefore does not renew into the original's live lease.

        Residual (not detectable locally, 11A.3/11A.5): a block-level clone (VM image, ``asr``/``dd``) keeps the inode
        and every byte; so does a writable snapshot or clone attached on the same machine (btrfs/ZFS snapshot,
        ``asr``/``dd`` clone as a second volume), now that ``st_dev`` is not compared (round 5 P1); and a copy onto
        another volume can land on the same inode by chance. Such a clone passes 1 to 3 and renews the original's live
        ``gen``; the relay's holder ``gen`` is the only backstop. Never run a clone while the original is live.

        Then:

        4. Renew with the stored ``gen`` (a renewal is not a probe); success: run.
        5. Only if fenced: write ``pending = gen + 1`` durably, then probe ``{gen: null, take_over: false}``;
           ``holder_conflict`` (a live lease) is waited out, never stolen.
        6. The granted ``gen`` must be exactly ``stored + 1`` (each new hold is +1 on the relay's row): then persist it
           and clear ``pending`` atomically before anything else. Any other value: alarm, write nothing under it, stop
           (the lease lapses in 40 s).
        7. An answer that carries no ``gen``: alarm.

        A state directory that never held (no ``holder.json``) acquires with ``{gen: null}`` (the first link), unless
        ``link.json`` says it was already linked (round 7, R6-3: refused, operator path)."""
        if self._fenced:
            raise Fenced("fenced: writes stopped")
        if self._gen is not None:
            return self._renew(self._gen, deadline_s, running=True)
        rec = self.preflight()
        if rec is None:
            return self._probe(None, deadline_s)
        assert rec.gen is not None
        try:
            return self._renew(rec.gen, deadline_s, running=False)
        except RelayRefused as e:
            if not (e.status == 409 and e.code == "fenced"):
                raise
        assert rec.gen is not None
        expected = rec.gen + 1
        self._holder.save(HolderRecord(True, rec.gen, rec.instance_id, expected))  # durable before the probe
        return self._probe(expected, deadline_s)

    def preflight(self) -> HolderRecord | None:
        """The one local preflight (11A.5 steps 1 to 3; round 6, orch-52 #1), run before **any** network call: the
        first :meth:`call` of a session (login, challenge, ``PUT card`` without a session, every directory read or
        write) runs it, :meth:`hold` at start runs it again every time, and :func:`orch.relay.link.first_link` runs it
        right after its ``link.json`` state check. Checks, in order:

        1. the instance record is bound to this very directory (a copied or restored directory: refused);
        2. no ``pending`` takeover record;
        3. (a) this state directory was the last holder and (b) ``holder.json`` carries this directory's instance id,
           and a stored ``gen`` exists.

        A directory that never held (no ``holder.json``) passes 1 and returns ``None`` (the first link), unless its
        history says otherwise (round 7, R6-3: :meth:`HolderStore.history_problem`; ``link.json`` already ``linked``
        but ``holder.json`` and/or ``instance.json`` missing is a restored or copied directory, not a first link). A
        failure alarms ``relay_takeover_refused`` and raises :class:`TakeoverRefused`; the session then refuses every
        later call locally, whatever passed before (round 7, R6-1), until :meth:`operator_takeover`. A
        :class:`StateError` (unreadable or older-format state) propagates and nothing is sent either. Returns the
        holder record that passed.

        The refusal (and the pass) is per :class:`Session` object (round 8, R7-4): R5 keeps ONE Session per state
        directory (shared by its relay threads), or shares the refusal state between them; a refusal raised in one
        Session does not stop another Session that already passed. Within one Session the whole preflight runs under a
        lock (round 9), so threads sharing it cannot interleave: a passing run never sets the pass after another
        thread's StateError, and the next call runs the preflight again.

        One accepted exception: :meth:`logout` still sends after a preflight StateError (not after a refusal), since
        releasing our own token is not acting as holder."""
        with self._preflight_lock:
            if self._refused is not None:
                raise TakeoverRefused(self._refused)
            try:
                return self._preflight_checks()
            except _Refusal as r:
                why = r.why
        # the alarm is raised after the lock is released (round 10, R9-3): a sink that hands work to another thread
        # using this Session, and waits for it, does not block on the lock. The refusal is already recorded.
        self._alarm("relay_takeover_refused", why)
        raise TakeoverRefused(why)

    def _preflight_checks(self) -> HolderRecord | None:
        """The body of :meth:`preflight`, run under ``_preflight_lock``; a failure raises :class:`_Refusal`."""
        # Not passed until this run passes (round 8, R7-2): a StateError raised now, after an earlier pass, makes
        # every later call run the preflight again, and fail again while the state stays unreadable.
        self._checked = False
        why = self._holder.binding_problem()
        if why is not None:
            self._refuse_locked(f"{why}; confirm with `orch host relay takeover`")
        rec = self._holder.load()
        if rec is None:
            why = self._holder.history_problem()
            if why is not None:
                self._refuse_locked(f"{why}; recover with `orch host relay takeover`")
        else:
            if rec.pending is not None:
                self._refuse_locked("pending takeover record: possibly our own interrupted takeover; confirm with "
                                    "`orch host relay takeover`")  # fmt: skip
            if not rec.last_holder:
                self._refuse_locked("(a) holder.json does not say this state directory was the last holder")
            if not wire.ct_equal(rec.instance_id.encode(), self._holder.instance_id().encode()):
                self._refuse_locked(
                    "(b) holder.json was written under another instance id (a restored or copied state directory)"
                )
            if rec.gen is None:
                self._refuse_locked("no stored gen to check the takeover against")
        self._checked = True
        return rec

    def operator_takeover(self, *, deadline_s: float = SMALL_DEADLINE_S) -> Hold:
        """The explicit operator takeover (R5 wires it to ``orch host relay takeover``): ``take_over: true``, then the
        new ``gen`` is recorded as ours (clearing ``pending``) and the worker may proceed. A directory whose instance
        record is bound elsewhere (copied or restored) gets a new instance id bound to it first."""
        try:
            stale = self._holder.binding_problem() is not None
        except StateError:  # an unreadable or older-format instance.json: the operator confirmed, so rebind it
            stale = True
        if stale:
            self._holder.regenerate()
        with self._preflight_lock:
            self._checked, self._refused = True, None  # the operator's confirmation stands in for the preflight
        got = self._hold_call({"gen": None, "take_over": True}, deadline_s, takeover=True)
        self._holder.save(HolderRecord(True, got.gen, self._holder.instance_id(), None))
        self._gen, self._fenced = got.gen, False
        return got

    def _refuse_locked(self, why: str) -> None:
        """Record the refusal under ``_preflight_lock``; :meth:`preflight` alarms once the lock is released."""
        self._fenced = True
        self._refused = why
        raise _Refusal(why)

    def _refuse(self, why: str) -> None:
        self._fenced = True
        self._refused = why
        self._alarm("relay_takeover_refused", why)
        raise TakeoverRefused(why)

    def _hold_call(
        self,
        body: dict[str, Any],
        deadline_s: float,
        *,
        takeover: bool,
        fenced_stops: bool = True,
        transport: Transport | None = None,
    ) -> Hold:
        path = f"/v2/workspaces/{self.workspace_id}/host/hold"
        route = "POST /v2/workspaces/{ws}/host/hold"
        try:
            obj = self.call(
                "POST", path, route, body=body, deadline_s=deadline_s, keys={"gen", "lease_s", "cursor"},
                fenced_stops=fenced_stops, transport=transport,
                resend_ok=body.get("gen") is not None,  # an acquire / probe / takeover is not idempotent
            )  # fmt: skip
            return Hold(_int(obj["gen"], 1), _int(obj["lease_s"], 1), _int(obj["cursor"]))
        except RelayMisbehaving as e:
            if takeover:
                self._refuse(f"the relay's hold answer carried no valid gen ({e})")
            raise

    def renew(self, *, deadline_s: float = AUX_HOLD_DEADLINE_S, transport: Transport | None = None) -> Hold:
        """Renew the RUNNING lease (``{gen, take_over: false}``; a ``fenced`` answer stops, as a running renewal does)
        over the caller's own connection. R3's first link uses it: R5 starts the aux renewal only after the link, and
        a first publication can outlast the 40 s lease (review R2). Never a probe: without a generation it refuses."""
        if self._fenced:
            raise Fenced("fenced: writes stopped")
        gen = self._gen
        if gen is None:
            raise NotHolder("no holder generation to renew")
        return self._renew(gen, deadline_s, running=True, transport=transport)

    def _renew(self, gen: int, deadline_s: float, *, running: bool, transport: Transport | None = None) -> Hold:
        got = self._hold_call(
            {"gen": gen, "take_over": False}, deadline_s, takeover=False, fenced_stops=running, transport=transport
        )
        if got.gen != gen:
            raise RelayMisbehaving("hold renewed another generation")
        self._gen = gen
        return got

    def _probe(self, expected: int | None, deadline_s: float) -> Hold:
        while True:
            try:
                got = self._hold_call({"gen": None, "take_over": False}, deadline_s, takeover=expected is not None)
            except RelayRefused as e:
                if e.status == 409 and e.code == "holder_conflict":
                    log.info("relay holder_conflict; waiting out the lease (never stolen)")
                    self._wait(LEASE_S)
                    continue
                raise
            break
        iid = self._holder.instance_id()
        if expected is not None and got.gen != expected:
            self._holder.save(HolderRecord(False, expected - 1, iid, None))
            self._refuse(
                f"relay gen {got.gen} != stored+1 ({expected}): another holder took in between, or a restored state"
            )
        self._holder.save(HolderRecord(True, got.gen, iid, None))  # before any other write or poll
        self._gen = got.gen
        return got

    def _wait(self, seconds: float) -> None:
        """Wait ``seconds``; with a stop event, the wait ends as soon as stop is set (11A.7: join within 5 s)."""
        if self._stop is None:
            self._sleep(seconds)
            return
        if self._stop.wait(seconds):
            raise SessionError("stopping")
