import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from . import trusted as trust
from .policy import LOOPBACK, REMOTE

# A launch code lives 30 s; its redirect file lives exactly as long (#542 item 3). Redemption and the expiry timer
# both go through `_forget` under the lock: the code is invalidated and the file unlinked exactly once, by whichever
# comes first. A file a paused launcher writes after that holds a dead code (and the launcher refuses to write or keep
# one past `expires_at`); the next server start's sweep removes any leftover.
CODE_TTL = 30
# The one-time nonce of `orch setup` lives 5 min: the person may copy the printed link into another terminal or
# browser. Still single use, and the claim still binds the first browser; only the window to present it is longer.
SETUP_CODE_TTL = 300
# The code `--print-url` prints on a headless machine (#1198 R1-1) lives 5 min too: the person has to open an
# `ssh -L` tunnel on their own computer before they can use it. Still single use; desktops keep CODE_TTL.
TUNNEL_CODE_TTL = 300

#: Session lifetimes in seconds, (absolute, idle), by how the session was opened. A kind this table does not know gets
#: the local numbers (fail closed on the route gates, not here: an unknown kind is never "local" anywhere else).
# remote: 12 h absolute, 2 h idle (host spec 0.8.6 10.3); switch (H1): the same
LIFETIMES = {"local": (86400, 43200), "remote": (43200, 7200), "switch": (43200, 7200)}
#: The listener a kind of session belongs to. A session works only on the listener that opened it (#1198 R1-4): a
#: remote bearer presented on loopback (or a local one on the front) is as unknown as a made-up token.
KIND_LISTENER = {"local": LOOPBACK, "remote": REMOTE}
#: Kinds a launch code can open: a launch code is made on this computer and is a loopback credential.
LAUNCH_KINDS = frozenset({"local"})
#: A passkey step-up (verified by `/api/stepup/verify`) stays usable this long, once, for the op it was made for (owner
#: decision of 24 Oct 2026: a fresh touch at most every 10 minutes). At most this many wait per session.
STEPUP_FRESH = 600
STEPUPS_PER_SESSION = 8
CHALLENGE_TTL = 60
CHALLENGES_PER_SESSION = 10

#: Passkey-login buckets, separate from the launch limiter so internet traffic cannot lock out the local owner (host
#: spec 0.8.6 10.3): 20 per minute per source, 600 per minute overall (a CPU guard only).
REMOTE_PER_SOURCE = 20
REMOTE_GLOBAL = 600
#: Live remote sessions are capped (#1198 R1-5): at most 20 in all and 5 per source, the oldest evicted first, so
#: logins from the internet cannot pile up state, and one source cannot push the owner's own session out alone.
REMOTE_SESSIONS = 20
SWITCH_SESSIONS = 20  # H1: sessions opened by a hub switch ticket
REMOTE_SESSIONS_PER_SOURCE = 5
#: Tracked sources are bounded; past this, new sources share one bucket (shared means stricter, never looser).
REMOTE_MAX_SOURCES = 10_000
_OVERFLOW = "overflow"


def client_address(value) -> str | None:
    """A client address as the front reports it, in one canonical spelling (an IPv4-mapped IPv6 address is the IPv4
    address). Anything that is not an address is None: the caller refuses the request."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 45:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def source_key(address) -> str | None:
    """The bucket a client address belongs to: an IPv4 address itself (/32) or its IPv6 /64 prefix."""
    address = client_address(address)
    if address is None:
        return None
    ip = ipaddress.ip_address(address)
    if ip.version == 4:
        return address
    return str(ipaddress.ip_network((int(ip) >> 64 << 64, 64)))


class Auth:
    def __init__(self, clock=time.time, directory=None, trust_file=None):
        self.directory = directory
        self.clock = clock
        #: browser keys enrolled by a real local sign-in (trusted.py): what upgrades a switch session
        self.trusted = trust.TrustedKeys(trust_file, clock)
        #: switch session -> (nonce, expires): the one challenge a browser key must sign (single use)
        self.challenges: dict[str, tuple[str, float]] = {}
        self.challenge_count: dict[str, int] = {}
        #: session upgraded by a browser key -> (key id, its switch workspace); revoking the key puts it back
        self.upgraded: dict[str, tuple[str, str]] = {}
        self.instance = secrets.token_hex(16)
        self.probe_key = secrets.token_hex(32)
        self.codes: dict[str, float] = {}
        self.setup_codes: set[str] = set()  # codes minted for the setup page: they never open a normal session
        # The exact file handed out per code, so later directory resolution can never change what is deleted.
        self.files: dict[str, Path] = {}
        self.sessions: dict[str, tuple[float, float]] = {}
        # How each session was opened: "local" (a launch code made by `orch dashboard` on this computer) or "remote"
        # (a passkey login on the front, `open_remote`). The kind is passed explicitly, never defaulted: the
        # local-only gates (One click, device key, runner, grant secrets) read it. The page shows "Log out" only for
        # non-local kinds.
        self.kinds: dict[str, str] = {}
        # The listener each session was opened on; Security refuses the token on any other (see KIND_LISTENER).
        self.listeners: dict[str, str] = {}
        self.switch_ws: dict[str, str] = {}  # switch session -> the one workspace its ticket was for (H1)
        self.session_sources: dict[str, str] = {}  # remote session -> the source bucket that opened it
        #: remote session -> {(action, digest of the op) -> (verified at, the proof the signer checks again, the
        #: credential that made it)}; single use
        self.stepups: dict[str, dict[tuple[str, str], tuple[float, object, str | None]]] = {}
        #: remote session -> the credential id (base64url) of the passkey that opened it
        self.credentials: dict[str, str] = {}
        #: credential ids of removed passkeys: a login or step-up verified before the removal cannot register after it
        #: (`open_remote`, `stepup_grant` refuse them). Enrolling the same id again clears it (`allow_credential`).
        self.revoked: set[str] = set()
        self.remote_sources: dict[str, deque] = {}
        self.remote_all = deque()
        self.remote_swept = 0.0
        self.setup_sessions: dict[str, str] = {}
        self.attempts = deque()
        self.lock = threading.Lock()

    def probe(self, nonce):
        return hmac.new(bytes.fromhex(self.probe_key), (nonce + self.instance).encode(), hashlib.sha256).hexdigest()

    def launch(self, port, setup=False, tunnel=False):
        with self.lock:
            now = self.clock()
            for stale in [c for c, until in self.codes.items() if until <= now]:
                self._forget(stale)
            code = secrets.token_urlsafe(32)
            ttl = SETUP_CODE_TTL if setup else TUNNEL_CODE_TTL if tunnel else CODE_TTL
            self.codes[code] = now + ttl
            if setup:
                self.setup_codes.add(code)
            payload = {
                "instance": self.instance,
                "port": port,
                "probe_key": self.probe_key,
                "code": code,
                # the launcher writes no file, and keeps none, past this; whole seconds (a frame carries no floats),
                # rounded down so the launcher stops early, never late
                "expires_at": int(now + ttl),
            }
            if self.directory is not None:
                self.files[code] = Path(self.redirect_path(code))
                payload["redirect_file"] = str(self.files[code])
                timer = threading.Timer(ttl, lambda: self.expire(code))
                timer.daemon = True
                timer.start()
            return payload

    def _attempt(self, now):
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            return False
        self.attempts.append(now)
        return True

    def redirect_path(self, code):
        return self.directory / ("launch-" + hashlib.sha256(code.encode()).hexdigest() + ".html")

    def _forget(self, code):
        """Hold ``self.lock``. The code is no longer redeemable and its recorded file is unlinked, once."""
        self.codes.pop(code, None)
        self.setup_codes.discard(code)
        path = self.files.pop(code, None)
        if path is not None:
            path.unlink(missing_ok=True)

    def expire(self, code):
        """The timer at the code's expiry: invalidate it and remove its file, unless redemption already did."""
        with self.lock:
            self._forget(code)

    def exchange_setup(self, nonce):
        """The one-time browser nonce of ``orch setup`` (a launch code minted for setup): constant-time compare,
        single use, 5 min. True only for the first browser that presents it; it never opens a normal session."""
        with self.lock:
            now = self.clock()
            found = None
            for code in self.setup_codes:  # compare against every candidate: no early exit on a prefix
                if hmac.compare_digest(code.encode(), str(nonce).encode()):
                    found = code
            if found is None:
                self._attempt(now)
                return False
            until = self.codes.get(found, 0)
            self._forget(found)  # single use, whether or not it is still alive
            return now < until

    def exchange(self, code, kind):
        """Redeem a launch code. ``kind`` is required (no default): a launch code is a local credential, so only the
        kinds in LAUNCH_KINDS can come out of it, and the session belongs to the loopback listener."""
        if kind not in LAUNCH_KINDS:
            raise ValueError("a launch code opens a local session only")
        with self.lock:
            now = self.clock()
            if code in self.setup_codes:
                return None, 401 if self._attempt(now) else 429  # a setup nonce is not a login code
            until = self.codes.get(code, 0)
            if now >= until:
                if code in self.codes:
                    self._forget(code)  # expired: dead, and its file goes now rather than at the timer
                return None, 401 if self._attempt(now) else 429
            self._forget(code)
            return self._mint(now, kind), 200

    def open_remote(self, source=None, credential_id=None):
        """A session of the front, called only after a passkey login (R1-5) has been verified. Lives 12 h / 2 h idle and
        works on the front listener only. ``source`` is the client address the front reported: at most
        ``REMOTE_SESSIONS_PER_SOURCE`` live sessions per source (its /32 or /64) and ``REMOTE_SESSIONS`` in all, the
        oldest evicted first. ``credential_id`` is the passkey that logged in: removing it ends this session, and a
        removed one opens none (returns None)."""
        key = source_key(source) if source is not None else None
        with self.lock:
            if credential_id in self.revoked:
                return None
            now = self.clock()
            self._drop_dead(now)
            self._evict("remote", REMOTE_SESSIONS - 1)
            if key is not None:
                self._evict("remote", REMOTE_SESSIONS_PER_SOURCE - 1, key)
            minted = self._mint(now, "remote")
            if key is not None:
                self.session_sources[minted["token"]] = key
            if credential_id is not None:
                self.credentials[minted["token"]] = credential_id
            return minted

    def open_switch(self, listener, workspace):
        """H1 (27 Oct): a session born from a hub switch ticket (the hub vouched for the workspace owner; the browser
        presented a one-use ticket). Kind ``switch`` is NOT ``local``: the local-only gates and the step-up rule treat
        it like a remote session (a passkey touch for the sensitive actions). It works on the listener it was
        redeemed on, 12 h / 2 h idle like a remote one; at most ``SWITCH_SESSIONS`` live, the oldest evicted first."""
        with self.lock:
            now = self.clock()
            self._drop_dead(now)
            self._evict("switch", SWITCH_SESSIONS - 1)
            minted = self._mint(now, "switch", listener)
            self.switch_ws[minted["token"]] = workspace
            return minted

    def switch_workspace(self, token):
        """The one workspace a switch session may read, or None for any other kind of session."""
        with self.lock:
            return self.switch_ws.get(token) if token in self.sessions else None

    # --- trusted browsers (run 12) ---

    def enrol_key(self, token, jwk, origin):
        """Enrol a browser key. ONLY a session opened by a real local sign-in may: not a switch session, not a remote
        one, not one a key upgraded. Returns the key id, or None."""
        clean = trust.clean_jwk(jwk)
        with self.lock:
            if (
                clean is None
                or token not in self.sessions
                or self.kinds.get(token) != "local"
                or token in self.upgraded
                or self.listeners.get(token) != LOOPBACK
            ):
                return None
            return self.trusted.enrol(clean, origin)

    def trusted_list(self):
        with self.lock:
            return self.trusted.listing()

    def revoke_key(self, kid):
        """Forget a browser key; every session it upgraded goes back to read-mostly (a switch session)."""
        with self.lock:
            found = self.trusted.remove(kid)
            self._downgrade_unknown_keys()
            return found

    def _downgrade_unknown_keys(self):
        """Hold ``self.lock``. A session upgraded by a key that is revoked or idle-expired is a switch session again."""
        self.trusted.sweep()
        for token, (kid, ws) in list(self.upgraded.items()):
            if kid in self.trusted.keys:
                continue
            del self.upgraded[token]
            if token in self.sessions:
                self.kinds[token] = "switch"
                self.switch_ws[token] = ws
                self.stepups.pop(token, None)

    def challenge(self, token):
        """A nonce for a switch session (loopback only, where keys are enrolled) to sign with its browser key; at most
        ``CHALLENGES_PER_SESSION`` per session. None when the session cannot upgrade."""
        with self.lock:
            now = self.clock()
            if (
                token not in self.sessions
                or self.kinds.get(token) != "switch"
                or self.listeners.get(token) != LOOPBACK
                or self.challenge_count.get(token, 0) >= CHALLENGES_PER_SESSION
            ):
                return None
            self.challenge_count[token] = self.challenge_count.get(token, 0) + 1
            nonce = secrets.token_urlsafe(32)
            self.challenges[token] = (nonce, now + CHALLENGE_TTL)
            return nonce

    def upgrade(self, token, kid, signature, origin):
        """The switch session proves it holds an enrolled browser key by signing its challenge (single use, whatever
        the outcome). Success: a NEW ``local`` session (the switch one ends), exactly what a launch link gives, and
        the key is remembered as its upgrader. None for anything else."""
        with self.lock:
            now = self.clock()
            held = self.challenges.pop(token, None)
            if (
                held is None
                or held[1] < now
                or token not in self.sessions
                or self.kinds.get(token) != "switch"
                or self.listeners.get(token) != LOOPBACK
                or not isinstance(kid, str)
                or not isinstance(signature, str)
            ):
                return None
            ws = self.switch_ws.get(token)
            row = self.trusted.get(kid)
            if (
                ws is None
                or row is None
                or row["origin"] != origin
                or not trust.verify(row["jwk"], held[0], origin, signature)
            ):
                return None
            self.trusted.touch(kid)
            self._drop(token)
            minted = self._mint(now, "local")
            self.upgraded[minted["token"]] = (kid, ws)
            return minted

    def switch_attempt(self, address):
        """One switch-ticket try: a remote source has its own bucket (like a passkey login); on loopback there is no
        client address, so the launch limiter (10 a minute) applies. False when the bucket is full."""
        if address is None:
            with self.lock:
                return self._attempt(self.clock())
        return self.remote_attempt(address)

    def _evict(self, kind, keep, source=None):
        """Hold ``self.lock``. Drop the oldest sessions of ``kind`` (of one ``source`` when given) until at most
        ``keep`` remain."""
        mine = sorted(
            (self.sessions[t][0], t)
            for t, k in self.kinds.items()
            if k == kind and t in self.sessions and (source is None or self.session_sources.get(t) == source)
        )
        for _, token in mine[: max(0, len(mine) - keep)]:
            self._drop(token)

    def _drop(self, token):
        """Hold ``self.lock``."""
        for table in (
            self.sessions,
            self.kinds,
            self.listeners,
            self.session_sources,
            self.stepups,
            self.credentials,
            self.switch_ws,
            self.challenges,
            self.challenge_count,
            self.upgraded,
        ):
            table.pop(token, None)

    def _drop_dead(self, now):
        """Hold ``self.lock``. Forget sessions past their lifetime (bounded state even if tabs never return)."""
        self.sessions = {
            t: (born, last)
            for t, (born, last) in self.sessions.items()
            if self._alive(self.kinds.get(t), born, last, now)
        }
        for table in (
            self.kinds,
            self.listeners,
            self.session_sources,
            self.stepups,
            self.credentials,
            self.switch_ws,
            self.challenges,
            self.challenge_count,
            self.upgraded,
        ):
            for token in [t for t in table if t not in self.sessions]:
                del table[token]

    def _mint(self, now, kind, listener=None):
        """Hold ``self.lock``."""
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now, now
        self.kinds[token] = kind
        self.listeners[token] = listener or KIND_LISTENER[kind]
        absolute = LIFETIMES[kind][0]
        return {"token": token, "expires_at": datetime.fromtimestamp(now + absolute, UTC).isoformat()}

    def _sweep_remote(self, now):
        """Hold ``self.lock``."""
        self.remote_swept = now
        for bucket in [self.remote_all, *self.remote_sources.values()]:
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
        self.remote_sources = {k: b for k, b in self.remote_sources.items() if b}

    def remote_attempt(self, address):
        """One passkey-login attempt from ``address`` (the front's X-Orch-Client). False when the source's bucket
        (20 per minute) or the global one (600 per minute) is full; a refused attempt is not counted again.
        Never touches the launch limiter."""
        key = source_key(address)
        if key is None:
            return False
        with self.lock:
            now = self.clock()
            while self.remote_all and self.remote_all[0] <= now - 60:  # amortised O(1)
                self.remote_all.popleft()
            if len(self.remote_all) >= REMOTE_GLOBAL:  # the cheapest check first: a flood costs one comparison
                return False
            if now - self.remote_swept >= 1:  # draining every bucket on every attempt would be work an attacker sets
                self._sweep_remote(now)
            elif key in self.remote_sources:
                bucket = self.remote_sources[key]
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
            if key not in self.remote_sources and len(self.remote_sources) >= REMOTE_MAX_SOURCES:
                key = _OVERFLOW
            bucket = self.remote_sources.setdefault(key, deque())
            if len(bucket) >= REMOTE_PER_SOURCE:
                if not bucket:
                    del self.remote_sources[key]
                return False
            bucket.append(now)
            self.remote_all.append(now)
            return True

    def valid(self, token, touch=True):
        with self.lock:
            now = self.clock()
            # Bound retained state even if expired tabs never return.
            self._drop_dead(now)
            self._downgrade_unknown_keys()
            if token not in self.sessions:
                return False
            born, _ = self.sessions[token]
            if touch:
                self.sessions[token] = born, now
            return True

    @staticmethod
    def _alive(kind, born, last, now):
        absolute, idle = LIFETIMES.get(kind, LIFETIMES["local"])
        return now - born < absolute and now - last < idle

    def login_kind(self, token):
        """How this session was opened (``"local"`` or ``"remote"``), or None for an unknown session."""
        with self.lock:
            return self.kinds.get(token) if token in self.sessions else None

    def stepup_grant(self, token, action, digest, proof, credential_id=None):
        """A verified passkey step-up for ``action`` with ``digest``, kept for this session only (never for another
        token). A newer one for the same pair replaces the older; the oldest goes beyond ``STEPUPS_PER_SESSION``.
        ``credential_id`` is the passkey that made it: removing that passkey drops it."""
        with self.lock:
            if token not in self.sessions or credential_id in self.revoked:
                return False
            held = self.stepups.setdefault(token, {})
            now = self.clock()
            for stale in [d for d, (at, _, _) in held.items() if now - at >= STEPUP_FRESH]:
                del held[stale]
            held.pop((action, digest), None)
            while len(held) >= STEPUPS_PER_SESSION:
                del held[next(iter(held))]
            held[(action, digest)] = (now, proof, credential_id)
            return True

    def stepup_take(self, token, action, digest):
        """``(True, proof)`` once for a step-up of this session for exactly (``action``, ``digest``) that is at most
        ``STEPUP_FRESH`` old, else ``(False, None)``. Taking spends it, whatever happens to the op afterwards."""
        with self.lock:
            if token not in self.sessions:
                return False, None
            entry = self.stepups.get(token, {}).pop((action, digest), None)
            if entry is None or self.clock() - entry[0] >= STEPUP_FRESH:
                return False, None
            return True, entry[1]

    def allow_credential(self, credential_id):
        """A passkey with this id was enrolled (again): it is no longer revoked. A new enrollment is a new passkey
        even when an authenticator reuses the id."""
        with self.lock:
            self.revoked.discard(credential_id)

    def drop_credential(self, credential_id):
        """A passkey was removed: end every session it opened (the requesting one too, if it used it) and drop every
        step-up it made in any session."""
        with self.lock:
            self.revoked.add(credential_id)
            for token in [t for t, c in self.credentials.items() if c == credential_id]:
                self._drop(token)
            for held in self.stepups.values():
                for key in [k for k, (_, _, c) in held.items() if c == credential_id]:
                    del held[key]

    def listener_of(self, token):
        """The listener this session was opened on (``"loopback"`` or ``"remote"``), or None when unknown. A session
        without a recorded listener (a test that wrote ``kinds`` by hand) belongs to loopback."""
        with self.lock:
            return self.listeners.get(token, LOOPBACK) if token in self.sessions else None

    def logout(self, token):
        with self.lock:
            self._drop(token)

    def logout_everywhere(self, kind="remote"):
        """End every session of ``kind`` ("Log out everywhere" in Settings, R1-8); a remote logout ends the sessions
        opened by a hub switch too."""
        with self.lock:
            kinds = {kind, "switch"} if kind == "remote" else {kind}
            for token in [t for t, k in self.kinds.items() if k in kinds]:
                self._drop(token)

    def bind_setup(self, token, setup_id):
        """Setup scope only. Core owns the 30 s single-use claim and 10 min idle/run-aware lifetime.

        Retain only a credential hash and setup id; never any setup request or response body.
        """
        with self.lock:
            self.setup_sessions.clear()  # one setup process / bound browser per local socket
            self.setup_sessions[hashlib.sha256(token.encode()).hexdigest()] = setup_id

    def valid_setup(self, token):
        with self.lock:
            return bool(token) and hashlib.sha256(token.encode()).hexdigest() in self.setup_sessions

    def forget_setup(self, token):
        with self.lock:
            self.setup_sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
