"""Trusted browsers (run 12, "switch is navigation; authority stays with the workspace").

A normal sign-in on this computer (a launch link, kind ``local``) enrols the browser: the page makes an ECDSA P-256 key
pair with WebCrypto, keeps the private half non-extractable in IndexedDB (origin-scoped: another port is another
origin, which a cookie would not give), and sends the public half here. A hub switch later proves possession of that
key to turn its read-only session into a full one (`Auth.upgrade`); without the key it stays read-mostly.

**Invariants**
* Only a session opened by a real local sign-in enrols a key (`Auth.enrol`): a switch session, a remote one or a
  session an earlier key upgraded never does, so a hub-made session cannot mint its own way up.
* A key signs a one-use nonce of the session that asks, bound to the origin the key was enrolled for.
* A key unused for `IDLE` seconds, or revoked, upgrades nothing.
* The store holds public keys only.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

IDLE = 30 * 86400  # a trusted browser idle this long is forgotten
MAX_KEYS = 20
_B64 = re.compile(r"^[A-Za-z0-9_-]{43}$")
DOMAIN = b"orch-switch-upgrade-v1\0"


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def clean_jwk(jwk) -> dict | None:
    """The public P-256 key in its closed shape, or None."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        return None
    x, y = jwk.get("x"), jwk.get("y")
    if not (isinstance(x, str) and isinstance(y, str) and _B64.fullmatch(x) and _B64.fullmatch(y)):
        return None
    try:
        ec.EllipticCurvePublicNumbers(
            int.from_bytes(_unb64(x), "big"), int.from_bytes(_unb64(y), "big"), ec.SECP256R1()
        ).public_key()
    except ValueError:
        return None
    return {"kty": "EC", "crv": "P-256", "x": x, "y": y}


def key_id(jwk: dict) -> str:
    """The RFC 7638 thumbprint, hex: the same key is the same id."""
    canon = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


def message(nonce: str, origin: str) -> bytes:
    return DOMAIN + nonce.encode() + b"\0" + origin.encode()


def verify(jwk: dict, nonce: str, origin: str, signature: str) -> bool:
    """``signature`` is WebCrypto's ECDSA output: r || s, 64 bytes, base64url."""
    try:
        raw = _unb64(signature)
        if len(raw) != 64:
            return False
        pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_unb64(jwk["x"]), "big"), int.from_bytes(_unb64(jwk["y"]), "big"), ec.SECP256R1()
        ).public_key()
        der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        pub.verify(der, message(nonce, origin), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError, KeyError):
        return False


class TrustedKeys:
    """Enrolled browser keys, kept in memory and (when ``path`` is given) in a 0600 file shared by every dashboard
    process of this state directory. Another process may change the file at any time, so: a read re-reads it when its
    modification time changed (writes replace the file whole, so a read is never torn), and a change takes an ``fcntl``
    lock on a sidecar file, re-reads, changes and writes, so a revoke in one process is never undone by another's save.
    `Auth` holds its own lock around every call."""

    def __init__(self, path: Path | None, clock):
        self.path, self.clock = path, clock
        self.keys: dict[str, dict] = {}
        self._seen: tuple | None = None
        self._refresh(force=True)

    def _stamp(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None

    def _refresh(self, force=False):
        if self.path is None:
            return
        stamp = self._stamp()
        if not force and stamp == self._seen:
            return
        self._seen = stamp
        keys: dict[str, dict] = {}
        with contextlib.suppress(OSError, ValueError, AttributeError):
            data = json.loads(self.path.read_text())
            for kid, row in (data.get("keys") or {}).items():
                jwk = clean_jwk(row.get("jwk"))
                if jwk and key_id(jwk) == kid:
                    keys[kid] = {
                        "jwk": jwk,
                        "origin": str(row.get("origin", ""))[:200],
                        "created": float(row.get("created", 0)),
                        "last_used": float(row.get("last_used", 0)),
                    }
        self.keys = keys

    @contextlib.contextmanager
    def _change(self):
        """Lock, re-read, let the caller change ``self.keys``, write."""
        if self.path is None:
            yield
            return
        lock = None
        try:
            with contextlib.suppress(OSError):
                lock = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
            self._refresh(force=True)
            yield
            self._save()
        finally:
            if lock is not None:
                os.close(lock)  # closing releases the lock

    def _save(self):
        if self.path is None:
            return
        with contextlib.suppress(OSError):  # the keys still work in memory; they are just not kept across a restart
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"keys": self.keys}, f)
            os.replace(tmp, self.path)
            self._seen = self._stamp()

    def sweep(self):
        self._refresh()
        now = self.clock()
        if not any(now - r["last_used"] >= IDLE for r in self.keys.values()):
            return
        with self._change():
            now = self.clock()
            for k in [k for k, r in self.keys.items() if now - r["last_used"] >= IDLE]:
                del self.keys[k]

    def enrol(self, jwk: dict, origin: str) -> str | None:
        self.sweep()
        kid = key_id(jwk)
        with self._change():
            now = self.clock()
            if kid not in self.keys and len(self.keys) >= MAX_KEYS:
                oldest = min(self.keys, key=lambda k: self.keys[k]["last_used"])
                del self.keys[oldest]
            row = self.keys.get(kid)
            self.keys[kid] = {"jwk": jwk, "origin": origin, "created": row["created"] if row else now, "last_used": now}
        return kid

    def get(self, kid: str) -> dict | None:
        self.sweep()
        return self.keys.get(kid)

    def touch(self, kid: str):
        with self._change():
            if kid in self.keys:
                self.keys[kid]["last_used"] = self.clock()

    def remove(self, kid: str) -> bool:
        with self._change():
            return self.keys.pop(kid, None) is not None

    def listing(self) -> list[dict]:
        self.sweep()
        return [
            {"id": k, "origin": r["origin"], "created": r["created"], "last_used": r["last_used"]}
            for k, r in sorted(self.keys.items(), key=lambda kv: -kv[1]["last_used"])
        ]
