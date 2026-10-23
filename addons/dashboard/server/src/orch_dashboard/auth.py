import hashlib
import hmac
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
        # How each session was opened. Every launch code is made by `orch dashboard` on this computer, so a session is
        # "local"; a hub or remote sign-in would pass its own kind to `exchange` (the page shows "Log out" only for those).
        self.kinds: dict[str, str] = {}
        self.setup_sessions: dict[str, str] = {}
        self.attempts = deque()
        self.lock = threading.Lock()

    def probe(self, nonce):
        return hmac.new(bytes.fromhex(self.probe_key), (nonce + self.instance).encode(), hashlib.sha256).hexdigest()

    def launch(self, port, setup=False):
        with self.lock:
            now = self.clock()
            for stale in [c for c, until in self.codes.items() if until <= now]:
                self._forget(stale)
            code = secrets.token_urlsafe(32)
            ttl = SETUP_CODE_TTL if setup else CODE_TTL
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

    def exchange(self, code, kind="local"):
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
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now, now
            self.kinds[token] = kind
            return {"token": token, "expires_at": datetime.fromtimestamp(now + 86400, UTC).isoformat()}, 200

    def valid(self, token, touch=True):
        with self.lock:
            now = self.clock()
            # Bound retained state even if expired tabs never return.
            self.sessions = {
                t: (born, last)
                for t, (born, last) in self.sessions.items()
                if now - born < 86400 and now - last < 43200
            }
            self.kinds = {t: k for t, k in self.kinds.items() if t in self.sessions}
            if token not in self.sessions:
                return False
            born, _ = self.sessions[token]
            if touch:
                self.sessions[token] = born, now
            return True

    def login_kind(self, token):
        """How this session was opened (``"local"``), or None for a session this server does not know."""
        with self.lock:
            return self.kinds.get(token) if token in self.sessions else None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
            self.kinds.pop(token, None)

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
