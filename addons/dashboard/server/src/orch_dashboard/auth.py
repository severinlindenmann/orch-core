import hashlib
import hmac
import ipaddress
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

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
LIFETIMES = {"local": (86400, 43200), "remote": (43200, 7200)}  # remote: 12 h absolute, 2 h idle (host spec 0.8.6 10.3)
#: The listener a kind of session belongs to. A session works only on the listener that opened it (#1198 R1-4): a
#: remote bearer presented on loopback (or a local one on the front) is as unknown as a made-up token.
KIND_LISTENER = {"local": "loopback", "remote": "remote"}
#: Kinds a launch code can open: a launch code is made on this computer and is a loopback credential.
LAUNCH_KINDS = frozenset({"local"})

#: Passkey-login buckets, separate from the launch limiter so internet traffic cannot lock out the local owner (host
#: spec 0.8.6 10.3): 20 per minute per source, 600 per minute overall (a CPU guard only).
REMOTE_PER_SOURCE = 20
REMOTE_GLOBAL = 600
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
    def __init__(self, clock=time.time, directory=None):
        self.directory = directory
        self.clock = clock
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
        self.remote_sources: dict[str, deque] = {}
        self.remote_all = deque()
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

    def open_remote(self):
        """A session of the front, called only after a passkey login (R1-5) has been verified. Lives 12 h / 2 h idle and
        works on the front listener only."""
        with self.lock:
            return self._mint(self.clock(), "remote")

    def _mint(self, now, kind):
        """Hold ``self.lock``."""
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now, now
        self.kinds[token] = kind
        self.listeners[token] = KIND_LISTENER[kind]
        absolute = LIFETIMES[kind][0]
        return {"token": token, "expires_at": datetime.fromtimestamp(now + absolute, UTC).isoformat()}

    def remote_attempt(self, address):
        """One passkey-login attempt from ``address`` (the front's X-Orch-Client). False when the source's bucket
        (20 per minute) or the global one (600 per minute) is full; a refused attempt is not counted again.
        Never touches the launch limiter."""
        key = source_key(address)
        if key is None:
            return False
        with self.lock:
            now = self.clock()
            for bucket in [self.remote_all, *self.remote_sources.values()]:
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
            self.remote_sources = {k: b for k, b in self.remote_sources.items() if b}
            if key not in self.remote_sources and len(self.remote_sources) >= REMOTE_MAX_SOURCES:
                key = _OVERFLOW
            bucket = self.remote_sources.setdefault(key, deque())
            if len(bucket) >= REMOTE_PER_SOURCE or len(self.remote_all) >= REMOTE_GLOBAL:
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
            self.sessions = {
                t: (born, last)
                for t, (born, last) in self.sessions.items()
                if self._alive(self.kinds.get(t), born, last, now)
            }
            self.kinds = {t: k for t, k in self.kinds.items() if t in self.sessions}
            self.listeners = {t: k for t, k in self.listeners.items() if t in self.sessions}
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

    def listener_of(self, token):
        """The listener this session was opened on (``"loopback"`` or ``"remote"``), or None when unknown. A session
        without a recorded listener (a test that wrote ``kinds`` by hand) belongs to loopback."""
        with self.lock:
            return self.listeners.get(token, "loopback") if token in self.sessions else None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
            self.kinds.pop(token, None)
            self.listeners.pop(token, None)

    def logout_everywhere(self, kind="remote"):
        """End every session of ``kind`` ("Log out everywhere" in Settings, R1-8)."""
        with self.lock:
            for token in [t for t, k in self.kinds.items() if k == kind]:
                self.sessions.pop(token, None)
                self.kinds.pop(token, None)
                self.listeners.pop(token, None)

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
