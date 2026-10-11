import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from datetime import UTC, datetime


class Auth:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.instance = secrets.token_hex(16)
        self.probe_key = secrets.token_hex(32)
        self.codes: dict[str, float] = {}
        self.sessions: dict[str, tuple[float, float]] = {}
        self.attempts = deque()
        self.lock = threading.Lock()

    def probe(self, nonce):
        return hmac.new(bytes.fromhex(self.probe_key), (nonce + self.instance).encode(), hashlib.sha256).hexdigest()

    def launch(self, port):
        with self.lock:
            now = self.clock()
            self.codes = {c: until for c, until in self.codes.items() if until > now}
            code = secrets.token_urlsafe(32)
            self.codes[code] = now + 60
            return {"instance": self.instance, "port": port, "probe_key": self.probe_key, "code": code}

    def _attempt(self, now):
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            return False
        self.attempts.append(now)
        return True

    def allow_attempt(self):
        with self.lock:
            return self._attempt(self.clock())

    def exchange(self, code, *, count_attempt=True):
        with self.lock:
            now = self.clock()
            if count_attempt and not self._attempt(now):
                return None, 429
            until = self.codes.pop(code, 0)
            if now >= until:
                return None, 401
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now, now
            return {"token": token, "expires_at": datetime.fromtimestamp(now + 86400, UTC).isoformat()}, 200

    def valid(self, token):
        with self.lock:
            now = self.clock()
            # Bound retained state even if expired tabs never return.
            self.sessions = {
                t: (born, last)
                for t, (born, last) in self.sessions.items()
                if now - born < 86400 and now - last < 43200
            }
            if token not in self.sessions:
                return False
            born, _ = self.sessions[token]
            self.sessions[token] = born, now
            return True

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
