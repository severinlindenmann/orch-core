"""Host-side port of the remote access contract R0 (hub protocol.md §10.2c, §8.5; orch-core #1588), test code only.

Written apart from the hub's reference (``orch_hub.contract.remote``) and from core's primitives only (``orch.crypto``,
``orch.canon.jcs``, the custodial checks of ``orch.hubclient``), so ``test_remote_vectors.py`` is a second, independent
reading of ``contracts/hub/vectors_remote.json``. R2 and R3 build the real host paths (stream gate, device list,
enrollment, Noise responder, presence in the click signer) and replay the same file against them; this port is what
they must agree with until then; R3's in-session part (app frames, exchanges, ``route_digest``, #1591) is at the end.
Every refusal raises :class:`orch.hubclient.errors.HubRefused`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from orch.canon.jcs import JcsError, dumps, loads_strict
from orch.crypto import suite
from orch.crypto.encoding import EncodingError, unb64u
from orch.hubclient.custodial_shape import text_ok
from orch.hubclient.errors import HubRefused

PROTOCOL = b"Noise_IK_25519_ChaChaPoly_SHA256"
LABEL = {
    "open": b"orch/v2/remote-open|",
    "presence": b"orch/v2/remote-presence|",
    "devices": b"orch/v2/sig/remote-devices|",
    "enroll": b"orch/v2/remote-enroll|",
    "prologue": b"orch/v2/remote-prologue|",
    "wrk": b"orch/v2/remote-wrk|",
    "fp": b"orch/v2/remote-device-fp|",
    "route": b"orch/v2/remote-route|",
}
LINK_LABEL = {"w2h": b"orch/v2/sig/hub-link|", "h2w": b"orch/v2/hub-route|"}
RV, KIND_BYTE, PKIND_BYTE = 1, {"session": 1, "enroll": 2}, {"platform": 1, "fido2": 2}
RD, RC, PERSON, HEX32 = (
    re.compile(p) for p in (r"rd_[0-9a-f]{32}", r"rc_[0-9a-f]{32}", r"p_[0-9a-f]{32}", r"[0-9a-f]{32}")
)
FRAME_MAX, MIN_COST, BATCH_MS, LINK_MS = 65535, 1024, 300_000, 120_000
LEVELS = ("view", "work", "terminals")
CODES = ("remote.off", "remote.device", "remote.busy", "remote.no_code", "remote.version")
REASONS = ("closed", "idle", "expired", "revoked", "handshake", "presence", "too_large", "malformed", *CODES)
MAX_SAFE = 2**53 - 1


def no(code: str = "malformed") -> HubRefused:
    return HubRefused(code)


def hx(s: Any, n: int) -> bytes:
    if type(s) is not str or len(s) != 2 * n or re.fullmatch("[0-9a-f]*", s) is None:
        raise no()
    return bytes.fromhex(s)


def b64(s: Any, n: int | None = None) -> bytes:
    try:
        return unb64u(s, n)
    except EncodingError:
        raise no() from None


def ident16(s: Any) -> bytes:
    if type(s) is not str or not (RD.fullmatch(s) or RC.fullmatch(s)):
        raise no()
    return bytes.fromhex(s[3:])


def nat(x: Any, lo: int = 0) -> bool:
    return type(x) is int and lo <= x <= MAX_SAFE


# --- Noise IK -------------------------------------------------------------------------------------------------------


def _mac(k: bytes, d: bytes) -> bytes:
    return hmac.new(k, d, hashlib.sha256).digest()


def _kdf(ck: bytes, ikm: bytes) -> tuple[bytes, bytes]:
    t = _mac(ck, ikm)
    a = _mac(t, b"\x01")
    return a, _mac(t, a + b"\x02")


def xpub(raw: bytes) -> bytes:
    return (
        X25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )


def _dh(priv: bytes, pub: bytes) -> bytes:
    try:
        return X25519PrivateKey.from_private_bytes(priv).exchange(X25519PublicKey.from_public_bytes(pub))
    except ValueError:
        raise no("handshake") from None


class Cipher:
    def __init__(self, k: bytes | None, n: int = 0) -> None:
        self.k, self.n = k, n

    def _go(self, ad: bytes, data: bytes, enc: bool) -> bytes:
        if self.k is None:
            return data
        nonce = bytes(4) + self.n.to_bytes(8, "little")
        aead = ChaCha20Poly1305(self.k)
        try:
            out = aead.encrypt(nonce, data, ad) if enc else aead.decrypt(nonce, data, ad)
        except InvalidTag:
            raise no("handshake") from None
        self.n += 1
        return out

    def seal(self, ad: bytes, pt: bytes) -> bytes:
        return self._go(ad, pt, True)

    def open(self, ad: bytes, ct: bytes) -> bytes:
        return self._go(ad, ct, False)


class Noise:
    """One IK handshake state (initiator or responder); ``hh`` and ``c_i2r``/``c_r2i`` after message 2."""

    def __init__(self, prologue: bytes, responder_pub: bytes) -> None:
        self.ck = self.h = PROTOCOL  # exactly 32 bytes: used as is
        self.c = Cipher(None)
        self.mix_h(prologue)
        self.mix_h(responder_pub)
        self.hh = b""
        self.c_i2r = self.c_r2i = Cipher(None)

    def mix_h(self, d: bytes) -> None:
        self.h = hashlib.sha256(self.h + d).digest()

    def mix_k(self, ikm: bytes) -> None:
        self.ck, k = _kdf(self.ck, ikm)
        self.c = Cipher(k)

    def enc(self, pt: bytes) -> bytes:
        ct = self.c.seal(self.h, pt)
        self.mix_h(ct)
        return ct

    def dec(self, ct: bytes) -> bytes:
        pt = self.c.open(self.h, ct)
        self.mix_h(ct)
        return pt

    def finish(self) -> None:
        self.hh = self.h
        k1, k2 = _kdf(self.ck, b"")
        self.c_i2r, self.c_r2i = Cipher(k1), Cipher(k2)


def prologue(kind: str, ws: str, ident: str, stream: str, hub: str) -> bytes:
    if kind not in KIND_BYTE or (RD if kind == "session" else RC).fullmatch(str(ident)) is None:
        raise no()
    return LABEL["prologue"] + bytes([RV, KIND_BYTE[kind]]) + hx(ws, 16) + ident16(ident) + hx(stream, 16) + hx(hub, 16)


def initiator1(pro: bytes, s: bytes, e: bytes, wrk_pub: bytes) -> tuple[Noise, bytes]:
    n = Noise(pro, wrk_pub)
    n.s, n.e = s, e  # type: ignore[attr-defined]
    ep = xpub(e)
    n.mix_h(ep)
    n.mix_k(_dh(e, wrk_pub))
    es = n.enc(xpub(s))
    n.mix_k(_dh(s, wrk_pub))
    return n, ep + es + n.enc(b"")


def responder1(pro: bytes, wrk: bytes, m1: bytes) -> tuple[Noise, bytes, bytes]:
    """Returns the state, the device's ephemeral and its static (transport) key."""
    if len(m1) != 96:
        raise no()  # no early data: message 1 is exactly e (32) || enc(s) (48) || tag of the empty payload (16)
    n = Noise(pro, xpub(wrk))
    re_ = m1[:32]
    n.mix_h(re_)
    n.mix_k(_dh(wrk, re_))
    rs = n.dec(m1[32:80])
    n.mix_k(_dh(wrk, rs))
    n.dec(m1[80:])
    return n, re_, rs


def responder2(n: Noise, re_: bytes, rs: bytes, e: bytes, challenge: bytes) -> bytes:
    if len(challenge) != 32:
        raise no()
    ep = xpub(e)
    n.mix_h(ep)
    n.mix_k(_dh(e, re_))
    n.mix_k(_dh(e, rs))
    out = ep + n.enc(challenge)
    n.finish()
    return out


def initiator2(n: Noise, m2: bytes) -> bytes:
    if len(m2) != 80:
        raise no()
    re_ = m2[:32]
    n.mix_h(re_)
    n.mix_k(_dh(n.e, re_))  # type: ignore[attr-defined]
    n.mix_k(_dh(n.s, re_))  # type: ignore[attr-defined]
    ch = n.dec(m2[32:])
    n.finish()
    return ch


# --- presence -------------------------------------------------------------------------------------------------------


def open_bytes(ws: str, ident: str, hh: bytes, ch: bytes) -> bytes:
    if len(hh) != 32 or len(ch) != 32:
        raise no()
    return LABEL["open"] + hx(ws, 16) + ident16(ident) + hh + ch


def presence_bytes(ws: str, dev: str, hh: bytes, nonce: bytes, digests: list[bytes]) -> bytes:
    ok = RD.fullmatch(str(dev)) and len(hh) == 32 and len(nonce) == 32 and 1 <= len(digests) <= 16
    if not ok or any(len(d) != 32 for d in digests) or digests != sorted(set(digests)):
        raise no()
    return LABEL["presence"] + hx(ws, 16) + ident16(dev) + hh + nonce + bytes([len(digests)]) + b"".join(digests)


def assertion_ok(pub_b64: str, kind: str, msg: bytes, a: Any, held: int) -> int:
    pub = b64(pub_b64, 65)
    if type(a) is not dict:
        raise no()
    want = {"p256": {"form", "sig"}, "ctap2": {"form", "auth_data", "sig"}}.get(a.get("form"))  # type: ignore[arg-type]
    if want is None or set(a) != want:
        raise no()
    sig = b64(a["sig"], 64)
    auth = b64(a["auth_data"], 37) if a["form"] == "ctap2" else b""
    if (a["form"] == "p256") != (kind == "platform"):
        raise no("factor_mismatch")
    if a["form"] == "p256":
        if not suite.verify(pub, sig, msg):
            raise no("bad_signature")
        return held
    rp_ok = auth[:32] == hashlib.sha256(b"orch-remote").digest()
    if not rp_ok or not suite.verify(pub, sig, auth + hashlib.sha256(msg).digest()):
        raise no("bad_signature")
    flags, count = auth[32], int.from_bytes(auth[33:], "big")
    if flags & 0xC0:
        raise no()
    if flags & 0x05 != 0x05 or flags & 0x18:
        raise no("factor_too_weak")  # UP+UV needed; BE/BS (synced passkey) refused
    if (held or count) and count <= held:
        raise no("replay")
    return count


def check_presence(a: Mapping[str, Any]) -> int:
    nonce = bytes.fromhex(a["nonce_hex"])
    t = {bytes.fromhex(k): v for k, v in a["issued"].items()}.get(nonce)
    if t is None or not t <= a["now_ms"] < t + BATCH_MS:
        raise no("replay")
    e = a["entry"]
    msg = presence_bytes(
        a["workspace_id"],
        e["device_id"],
        bytes.fromhex(a["handshake_hash_hex"]),
        nonce,
        [bytes.fromhex(d) for d in a["digests_hex"]],
    )
    return assertion_ok(e["presence_pub"], e["presence_kind"], msg, a["assertion"], a["held"])


# --- enrollment -----------------------------------------------------------------------------------------------------


def wrk_fp(wrk_pub: bytes) -> bytes:
    if len(wrk_pub) != 32:
        raise no()
    return hashlib.sha256(LABEL["wrk"] + wrk_pub).digest()[:16]


def code_bytes(code_id: str, secret: bytes, wrk_pub: bytes) -> bytes:
    if not RC.fullmatch(str(code_id)) or len(secret) != 16:
        raise no()
    return ident16(code_id) + secret + wrk_fp(wrk_pub)


def device_fp(tpub: bytes, ppub: bytes, kind: str) -> bytes:
    if len(tpub) != 32 or kind not in PKIND_BYTE:
        raise no()
    try:
        suite.validate_public_key(ppub)
    except Exception:
        raise no() from None
    return hashlib.sha256(LABEL["fp"] + bytes([PKIND_BYTE[kind]]) + tpub + ppub).digest()


def check_enroll(a: Mapping[str, Any]) -> dict[str, Any]:
    m = a["msg"]
    if type(m) is not dict or set(m) != {"t", "name", "presence_kind", "presence_pub", "mac", "presence"}:
        raise no()
    if m["t"] != "enroll" or not text_ok(m["name"], 64) or m["presence_kind"] not in PKIND_BYTE:
        raise no()
    ppub, mac = b64(m["presence_pub"], 65), b64(m["mac"], 32)
    tpub, hh = bytes.fromhex(a["transport_pub_hex"]), bytes.fromhex(a["handshake_hash_hex"])
    fp = device_fp(tpub, ppub, m["presence_kind"])  # also refuses a presence key off the curve
    body = LABEL["enroll"] + hh + tpub + ppub + bytes([PKIND_BYTE[m["presence_kind"]]])
    if not hmac.compare_digest(mac, _mac(bytes.fromhex(a["secret_hex"]), body)):
        raise no("bad_signature")
    msg = open_bytes(a["workspace_id"], a["code_id"], hh, bytes.fromhex(a["challenge_hex"]))
    n = assertion_ok(m["presence_pub"], m["presence_kind"], msg, m["presence"], 0)
    return {
        "name": m["name"],
        "presence_kind": m["presence_kind"],
        "presence_pub": m["presence_pub"],
        "transport_pub": tpub.hex(),
        "fingerprint": fp.hex(),
        "sign_count": n,
    }


def session_static(entry: Mapping[str, Any], rs: bytes) -> None:
    if not hmac.compare_digest(b64(entry["transport_pub"], 32), rs):
        raise no("handshake")


def check_open_msg(m: Any, host: list[int]) -> int:
    if type(m) is not dict or set(m) != {"t", "api", "presence"} or m["t"] != "open":
        raise no()
    api = m["api"]
    if type(api) is not list or len(api) != 2 or any(type(v) is not int or not 1 <= v < 2**31 for v in api):
        raise no()
    if api[0] > api[1]:
        raise no()
    if max(api[0], host[0]) > min(api[1], host[1]):
        raise no("remote.version")
    return min(api[1], host[1])


# --- device list ----------------------------------------------------------------------------------------------------


def _pub_ok(x: Any) -> bool:
    try:
        suite.validate_public_key(unb64u(x, 65))
    except Exception:
        return False
    return True


def _b64_ok(x: Any, n: int) -> bool:
    try:
        unb64u(x, n)
    except EncodingError:
        return False
    return True


ENTRY = {
    "device_id": lambda x: type(x) is str and RD.fullmatch(x) is not None,
    "name": lambda x: text_ok(x, 64),
    "person": lambda x: type(x) is str and PERSON.fullmatch(x) is not None,
    "transport_pub": lambda x: _b64_ok(x, 32),
    "presence_pub": _pub_ok,
    "presence_kind": lambda x: x in PKIND_BYTE,
    "level_cap": lambda x: x in LEVELS,
    "expires_ms": nat,
    "added_ms": nat,
    "confirmed_by": lambda x: type(x) is str and PERSON.fullmatch(x) is not None,
}


def check_device_list(signed: Any, wsk_pub: bytes, ws: str, held: int) -> dict[str, Any]:
    if type(signed) is not dict or set(signed) != {"o", "sig"} or type(signed["o"]) is not dict:
        raise no()
    o = signed["o"]
    if o.get("kind") != "remote.device_list":
        raise no()
    if type(o.get("suite")) is not int or o["suite"] != 2:
        raise no("wrong_suite")
    if o.get("v") != 2 or o.get("hv") != 0 or type(o.get("v")) is not int or type(o.get("hv")) is not int:
        raise no()
    try:
        body = dumps(o)
    except JcsError:
        raise no() from None
    if not suite.verify(wsk_pub, b64(signed["sig"], 64), LABEL["devices"] + body):
        raise no("bad_signature")
    top = {"v", "suite", "kind", "hv", "workspace_id", "version", "issued_ms", "devices"}
    if set(o) != top or type(o["workspace_id"]) is not str or not HEX32.fullmatch(o["workspace_id"]):
        raise no()
    if not nat(o["version"], 1) or not nat(o["issued_ms"]) or type(o["devices"]) is not list or len(o["devices"]) > 64:
        raise no()
    seen_keys: set[str] = set()
    ids = []
    for d in o["devices"]:
        if type(d) is not dict or set(d) != set(ENTRY) or not all(f(d[k]) for k, f in ENTRY.items()):
            raise no()
        if not d["added_ms"] < d["expires_ms"] <= d["added_ms"] + 366 * 86_400_000:
            raise no()
        for k in ("transport_pub", "presence_pub"):
            if d[k] in seen_keys:
                raise no()
            seen_keys.add(d[k])
        ids.append(d["device_id"])
    if ids != sorted(set(ids)):
        raise no()
    if o["workspace_id"] != ws:
        raise no("other_workspace")
    if o["version"] < held:
        raise no("stale_version")
    return o


def lookup(o: Mapping[str, Any], dev: str, now: int) -> dict[str, Any]:
    hit = [d for d in o["devices"] if d["device_id"] == dev and now < d["expires_ms"]]
    if not hit:
        raise no("remote.device")
    return hit[0]


# --- link stream frames ---------------------------------------------------------------------------------------------


def _frame_ok(x: Any) -> bool:
    try:
        return 1 <= len(unb64u(x)) <= FRAME_MAX
    except EncodingError:
        return False


BODIES = {
    "stream_open": {
        "stream_id": lambda x: type(x) is str and HEX32.fullmatch(x) is not None,
        "device_id": lambda x: type(x) is str and (RD.fullmatch(x) or RC.fullmatch(x)) is not None,
        "kind": lambda x: x in ("session", "enroll"),
    },
    "stream_data": {
        "stream_id": lambda x: type(x) is str and HEX32.fullmatch(x) is not None,
        "seq": lambda x: nat(x, 1),
        "bytes": _frame_ok,
    },
    "stream_close": {
        "stream_id": lambda x: type(x) is str and HEX32.fullmatch(x) is not None,
        "reason": lambda x: x in REASONS,
    },
}
LINK_FIELDS = ("v", "suite", "kind", "hv", "hub_id", "workspace_id", "dir", "seq", "ts_ms", "op", "re", "body")


def check_stream_link(a: Mapping[str, Any], pub: bytes) -> str:
    s = a["signed"]
    if type(s) is not dict or set(s) != {"o", "sig"} or type(s["o"]) is not dict or s["o"].get("kind") != "hub.link":
        raise no()
    o = s["o"]
    if type(o.get("suite")) is not int or o["suite"] != 2:
        raise no("wrong_suite")
    if not suite.verify(pub, b64(s["sig"], 64), LINK_LABEL[a["direction"]] + dumps(o)):
        raise no("bad_signature")
    if set(o) != set(LINK_FIELDS) or o["v"] != 2 or o["hv"] != 0 or o["dir"] != a["direction"]:
        raise no()
    if not (nat(o["seq"], 1) and nat(o["ts_ms"]) and type(o["body"]) is dict and type(o["op"]) is str):
        raise no()
    if o["op"] not in BODIES or o["re"] is not None or (o["op"] == "stream_open" and o["dir"] != "h2w"):
        raise no("unknown_op")
    if o["hub_id"] != a["hub_id"]:
        raise no("other_hub")
    if o["workspace_id"] != a["workspace_id"]:
        raise no("other_workspace")
    if abs(a["now_ms"] - o["ts_ms"]) > LINK_MS:
        raise no("stale_timestamp")
    spec, b = BODIES[o["op"]], o["body"]
    if set(b) != set(spec) or not all(f(b[k]) for k, f in spec.items()):
        raise no()
    if o["op"] == "stream_open" and (RD if b["kind"] == "session" else RC).fullmatch(b["device_id"]) is None:
        raise no()
    return o["op"]


def frame_cost(signed: Any) -> int:
    return max(MIN_COST, len(dumps(signed)))


# --- the stream_open gate -------------------------------------------------------------------------------------------


def gate(body: Mapping[str, Any], st: Mapping[str, Any], now: int) -> None:
    if body["stream_id"] in st["seen_streams"]:
        raise no("replay")
    if body["kind"] == "enroll":
        exp = st["codes"].get(body["device_id"])
        if exp is None or now >= exp:
            raise no("remote.no_code")
        if st["enrolls"] >= 3:
            raise no("remote.busy")
        return
    r = st["remote"]
    if not r["on"] or (r["expires_ms"] is not None and now >= r["expires_ms"]):
        raise no("remote.off")
    lookup(st["devices"], body["device_id"], now)
    if st["streams_device"] >= 4 or st["streams_workspace"] >= 16 or st["handshakes_device"] >= 10:
        raise no("remote.busy")


# --- R3: inside a session (app frames, exchanges, route_digest) -----------------------------------------------------
# Written from protocol.md §10.2c "Inside a session" alone; the hub reference's names are not reused on purpose.

KIND = (None, "HEAD", "BODY", "END", "RESET", "WS_OPEN", "WS_TEXT", "WS_BINARY", "WS_CLOSE", "PING", "PONG")
PLAIN_MAX = FRAME_MAX - 16
VERBS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
RESETS = frozenset({"cancelled", "timeout", "too_large", "remote.busy"})
NEVER_IN_HEAD = frozenset(
    "content-length transfer-encoding connection upgrade keep-alive te trailer proxy-connection sec-websocket-key "
    "sec-websocket-version sec-websocket-extensions sec-websocket-accept".split()
)
TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789!#$%&'*+-.^_`|~")
QUERY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~!$&'()*+,;=:@%/?")
MAX_HTTP, MAX_WS, MAX_REQ_BODY = 8, 2, 1 << 20


def _graphic(s: str) -> bool:
    return all(0x21 <= ord(c) <= 0x7E for c in s)


def plain_path(p: Any) -> bool:
    if type(p) is not str or not 0 < len(p) <= 2048 or p[:1] != "/" or not _graphic(p):
        return False
    if set(p) & set("%\\;?#"):
        return False
    return p == "/" or "" not in (segs := p[1:].split("/")) and "." not in segs and ".." not in segs


def query_ok(q: Any) -> bool:
    return type(q) is str and len(q) <= 4096 and set(q) <= QUERY_CHARS


def _header_list(h: Any, also_refused: set[str]) -> bool:
    if type(h) is not list or len(h) > 64:
        return False
    for item in h:
        if type(item) is not list or len(item) != 2 or type(item[0]) is not str or type(item[1]) is not str:
            return False
        n, v = item
        if not 0 < len(n) <= 64 or not set(n) <= TOKEN_CHARS or n in NEVER_IN_HEAD or n in also_refused:
            return False
        if len(v) > 4096 or not all(0x20 <= ord(c) <= 0x7E for c in v) or v != v.strip(" "):
            return False
    return True


def _cj_object(p: bytes, keys: set[str]) -> dict[str, Any]:
    if len(p) > 16384:
        raise no()
    try:
        o = loads_strict(p)
    except JcsError:
        raise no() from None
    if type(o) is not dict or set(o) != keys or dumps(o) != p:
        raise no()
    return o


def app_parse(d: str, pt: bytes) -> tuple[str, int, Any]:
    """(kind, id, value) of one app frame; value as the vectors write it (byte payloads by length)."""
    if not 5 <= len(pt) <= PLAIN_MAX or not 1 <= pt[0] <= 10:
        raise no()
    kind, ident, p = KIND[pt[0]], int.from_bytes(pt[1:5], "big"), pt[5:]
    if (kind in ("PING", "PONG")) is not (ident == 0):
        raise no()
    if kind in ("END", "PING", "PONG"):
        if p:
            raise no()
        return kind, ident, None
    if kind == "BODY":
        if not p:
            raise no()
        return kind, ident, len(p)
    if kind == "WS_BINARY":
        return kind, ident, len(p)
    if kind == "WS_TEXT":
        try:
            return kind, ident, p.decode("utf-8")
        except UnicodeDecodeError:
            raise no() from None
    if kind == "RESET":
        code = p.decode("latin-1")
        if code not in RESETS:
            raise no()
        return kind, ident, code
    if kind == "WS_CLOSE":
        c = int.from_bytes(p, "big") if len(p) == 2 else 0
        if not (1000 <= c <= 1003 or 1007 <= c <= 1011 or 3000 <= c <= 4999):
            raise no()
        return kind, ident, c
    if kind == "HEAD" and d == "h2d":
        o = _cj_object(p, {"status", "headers"})
        if type(o["status"]) is not int or not (o["status"] == 101 or 200 <= o["status"] <= 599):
            raise no()
        if not _header_list(o["headers"], {"set-cookie"}):
            raise no()
        return kind, ident, o
    if kind == "WS_OPEN" and d != "d2h":
        raise no()
    keys = {"path", "query", "headers"} | ({"method"} if kind == "HEAD" else set())
    o = _cj_object(p, keys)
    if kind == "HEAD" and o["method"] not in VERBS:
        raise no()
    if not plain_path(o["path"]) or not query_ok(o["query"]) or not _header_list(o["headers"], {"host", "origin"}):
        raise no()
    return kind, ident, o


def no_bearer(head: Mapping[str, Any]) -> None:
    if any(n in ("authorization", "cookie", "x-setup-token") or n.startswith("x-orch-") for n, _ in head["headers"]):
        raise no("remote.bearer")


class Exchanges:
    """Per-stream exchange state. Each side feeds its sent and received frames, in its local order, into ``step``,
    which gives "ok" / "drop" / "reset:<code>" or raises ``malformed``."""

    def __init__(self) -> None:
        self.top = 0
        self.live: dict[int, dict[str, Any]] = {}
        self.pinged = {"d2h": False, "h2d": False}

    def step(self, d: str, pt: bytes) -> str:
        kind, i, v = app_parse(d, pt)
        back = "h2d" if d == "d2h" else "d2h"
        if kind == "PING":
            if self.pinged[d]:
                raise no()
            self.pinged[d] = True
            return "ok"
        if kind == "PONG":
            if not self.pinged[back]:
                raise no()
            self.pinged[back] = False
            return "ok"
        x = self.live.get(i)
        if x is None:
            if d == "d2h" and kind in ("HEAD", "WS_OPEN"):
                if i <= self.top:
                    raise no()
                self.top = i
                ws = kind == "WS_OPEN"
                if sum(e["ws"] == ws for e in self.live.values()) >= (MAX_WS if ws else MAX_HTTP):
                    return "reset:remote.busy"
                self.live[i] = {"ws": ws, "up": None if ws else "open", "down": "wait", "n": 0, "shut": set()}
                return "ok"
            if i > self.top:
                raise no()
            return "drop"
        if kind == "RESET":
            del self.live[i]
            return "ok"
        side = "up" if d == "d2h" else "down"
        if side == "up" and not x["ws"] and x["up"] == "open" and kind in ("BODY", "END"):
            if kind == "END":
                x["up"] = "end"
            else:
                x["n"] += v
                if x["n"] > MAX_REQ_BODY:
                    del self.live[i]
                    return "reset:too_large"
        elif side == "down" and x["down"] == "wait" and kind == "HEAD":
            if v["status"] == 101 and not x["ws"]:
                raise no()
            x["down"] = "ws" if v["status"] == 101 else "open"
        elif side == "down" and x["down"] == "open" and kind in ("BODY", "END"):
            if kind == "END":
                x["down"] = "end"
        elif x["ws"] and x["down"] == "ws" and kind in ("WS_TEXT", "WS_BINARY", "WS_CLOSE") and d not in x["shut"]:
            if kind == "WS_CLOSE":
                x["shut"].add(d)
        else:
            raise no()
        if (x["up"] in (None, "end") and x["down"] == "end") or len(x["shut"]) == 2:
            del self.live[i]
        return "ok"


def route_preimage(ws: str, method: str, template: str, path: str, query: str, body: bytes) -> bytes:
    if method not in VERBS | {"WEBSOCKET"} or (method == "WEBSOCKET" and body):
        raise no()
    if type(template) is not str or not 0 < len(template) <= 1024 or not _graphic(template):
        raise no()
    if not plain_path(path) or not query_ok(query):
        raise no()
    parts = [LABEL["route"], hx(ws, 16), len(method).to_bytes(1, "big"), method.encode()]
    for s in (template, path, query):
        parts += [len(s).to_bytes(2, "big"), s.encode()]
    return b"".join(parts) + hashlib.sha256(body).digest()
