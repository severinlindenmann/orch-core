"""Pure codecs for the relay client (contract 11A.1, 11A.2, 11A.7): the origin, ``auth_bytes``, strict size-capped JSON
bodies, base64 fields checked for size before decoding, the ORB2 header and envelope, and ORB2 body sealing.

No I/O, no clock, no private key. Every function is total: bad input raises :class:`WireError`, whose message never
contains any of the input (11A.9 log redaction: the caller logs the class and route only).

Decisions:

* **Origin** (11A.2): ``scheme://host[:port]`` verbatim, lower-case ASCII, no path, query, fragment, userinfo or
  trailing slash, port 1..65535 without a leading zero. ``https`` always, ``http`` only for ``127.0.0.1`` and
  ``localhost`` (tests). It is signed exactly as stored; it is never derived from a response, redirect or DNS.
* **auth_bytes** is orch-relay ``proto.auth_bytes``: ``"orch/v2/sig/relay-auth|" || suite || lp16(origin) ||
  challenge(32) || u8(1 device | 2 workspace) || id(16)``.
* **JSON bodies** (11A.7): the whole body is capped before parsing; nesting is checked by a scan over the raw text
  (string contents skipped) before the parser runs, so a nesting bomb never reaches the recursive decoder; then the
  protocol §2.3 rules (no duplicate keys, floats, NaN, unsafe integers, non-ASCII or empty keys, lone surrogates,
  invalid UTF-8, BOM). HTTP bodies allow depth 32; signed, sealed and meta JSON keep the protocol's 16
  (:func:`loads_canonical`).
* **ORB2** (proto §8.1, §8.2): a 108-byte header that is the AAD and is signed; ``K_bridge(e) = HKDF(WK_e, "",
  "orch/v2/bridge|" hex(ws) "|" dec(e))``, ``K_msg = HKDF(channel, salt, "orch/v2/bridge-msg")``, body =
  AES-256-GCM(K_msg, zero nonce, plaintext, AAD = header). The envelope caps are the relay's (``bridge.envelope``):
  a request is at most 1 MiB and a response chunk at most 256 KiB, each counted over the **whole** envelope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag

from orch import canon, crypto
from orch.crypto.labels import L

__all__ = [
    "CHALLENGE_LEN",
    "PAGE_CAP",
    "PAGE_HEADS",
    "PageSpec",
    "build_manifest",
    "build_page",
    "check_hash",
    "manifest_bytes",
    "page_bytes",
    "page_digest",
    "plan_pages",
    "set_bytes",
    "sha256_ref",
    "HEADER_LEN",
    "MAX_BODY_DEPTH",
    "MAX_CHUNK",
    "MAX_META",
    "MAX_REQUEST",
    "OVERHEAD",
    "SUITE",
    "Header",
    "WireError",
    "auth_bytes",
    "b64_field",
    "bridge_signed_bytes",
    "build_envelope",
    "build_plaintext",
    "check_origin",
    "ct_equal",
    "dumps_body",
    "k_bridge",
    "k_msg",
    "k_pair",
    "loads_body",
    "loads_canonical",
    "loads_object",
    "open_body",
    "origin_parts",
    "parse_envelope",
    "parse_header",
    "parse_plaintext",
    "seal_body",
    "verify_signed",
]

SUITE = crypto.SUITE_ID
CHALLENGE_LEN = 32
HEADER_LEN = 108
TAG_LEN = 16
SIG_LEN = crypto.SIG_LEN
OVERHEAD = HEADER_LEN + TAG_LEN + SIG_LEN  # 188
MAX_REQUEST = 1 << 20
MAX_CHUNK = 256 * 1024
MAX_META = 65_536
MAX_BODY_DEPTH = 32
MAX_SAFE_INT = canon.jcs.MAX_SAFE_INT

FLAG_LAST, FLAG_STREAM, FLAG_REFUSAL = 0x01, 0x02, 0x04
_KINDS = {"device": 1, "workspace": 2}
_ZERO_NONCE = bytes(12)


class WireError(ValueError):
    """Bytes or values outside the wire contract. ``code`` is a relay-style code (``malformed``, ``too_large``);
    ``what`` names the field or rule. Neither ever contains input data."""

    def __init__(self, code: str, what: str) -> None:
        super().__init__(f"{code}: {what}")
        self.code, self.what = code, what


def _bad(what: str) -> WireError:
    return WireError("malformed", what)


# --- origin -----------------------------------------------------------------------------------------------------------

_ORIGIN = re.compile(
    r"(?P<scheme>https)://(?P<host>[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(?P<port>[1-9][0-9]{0,4}))?"
    r"|(?P<lscheme>http)://(?P<lhost>localhost|127\.0\.0\.1|10\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|172\.(?:1[6-9]|2[0-9]|3[01])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|192\.168\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])|100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9]))(?::(?P<lport>[1-9][0-9]{0,4}))?"
)


def check_origin(origin: object) -> str:
    """Return ``origin`` unchanged if it is a valid stored origin (11A.2), else raise."""
    if type(origin) is not str or not origin.isascii():
        raise _bad("origin")
    m = _ORIGIN.fullmatch(origin)
    if m is None:
        raise _bad("origin")
    port = m.group("port") or m.group("lport")
    if port is not None and int(port) > 65_535:
        raise _bad("origin")
    host = m.group("host") or ""
    if ".." in host or ".-" in host or "-." in host:
        raise _bad("origin")
    return origin


def origin_parts(origin: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` of a checked origin; the default port is filled in."""
    m = _ORIGIN.fullmatch(check_origin(origin))
    assert m is not None
    if m.group("scheme"):
        return "https", m.group("host"), int(m.group("port") or 443)
    return "http", m.group("lhost"), int(m.group("lport") or 80)


# --- relay login ------------------------------------------------------------------------------------------------------


def auth_bytes(origin: str, challenge: bytes, kind: str, ident: str) -> bytes:
    """The bytes a relay login signs (proto §12.2), byte-equal to orch-relay ``proto.auth_bytes``."""
    o = check_origin(origin).encode("ascii")
    if type(challenge) is not bytes or len(challenge) != CHALLENGE_LEN:
        raise _bad("challenge")
    if type(kind) is not str or kind not in _KINDS:
        raise _bad("kind")
    try:
        raw_id = crypto.unhex(ident, 16)
    except crypto.EncodingError:
        raise _bad("id") from None
    head = L["sig_relay_auth"] + bytes([SUITE]) + len(o).to_bytes(2, "big") + o
    return head + challenge + bytes([_KINDS[kind]]) + raw_id


# --- strict JSON ------------------------------------------------------------------------------------------------------

_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]', re.S)


def _check_depth(data: bytes, limit: int) -> None:
    """Refuse nesting deeper than ``limit`` by scanning the raw text (strings skipped), before any decoder runs."""
    if data.count(b"[") + data.count(b"{") <= limit:
        return
    depth = 0
    for m in _TOKEN.finditer(data):
        t = m.group()
        if t in (b"[", b"{"):
            depth += 1
            if depth > limit:
                raise _bad(f"nesting deeper than {limit}")
        elif t in (b"]", b"}"):
            depth -= 1


def _reject(_: str) -> Any:
    raise _bad("number or constant")


def _int(text: str) -> int:
    if len(text) > 17:
        raise _bad("integer")
    i = int(text)
    if not -MAX_SAFE_INT <= i <= MAX_SAFE_INT:
        raise _bad("integer")
    return i


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in pairs:
        if not k or not k.isascii():
            raise _bad("object key")
        if k in d:
            raise _bad("duplicate key")
        d[k] = v
    return d


def _strings_ok(o: Any) -> None:
    stack = [o]
    while stack:
        x = stack.pop()
        if type(x) is str:
            try:
                x.encode("utf-8")
            except UnicodeEncodeError:
                raise _bad("lone surrogate") from None
        elif type(x) is list:
            stack.extend(x)
        elif type(x) is dict:
            stack.extend(x.values())


def _loads(data: object, cap: int, depth: int) -> Any:
    if not isinstance(data, (bytes, bytearray)):
        raise _bad("body type")
    data = bytes(data)
    if len(data) > cap:
        raise WireError("too_large", "body over cap")
    _check_depth(data, depth)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad("utf-8") from None
    try:
        obj = json.loads(text, object_pairs_hook=_pairs, parse_float=_reject, parse_int=_int, parse_constant=_reject)
    except WireError:
        raise
    except (ValueError, RecursionError):
        raise _bad("json") from None
    _strings_ok(obj)
    return obj


def loads_body(data: bytes, *, cap: int) -> Any:
    """Parse an HTTP JSON body: at most ``cap`` bytes, depth <= 32, protocol §2.3 strictness."""
    return _loads(data, cap, MAX_BODY_DEPTH)


def loads_canonical(data: bytes, *, cap: int = MAX_REQUEST) -> Any:
    """Parse JSON that is signed, sealed or carried in a meta: the protocol subset, depth <= 16."""
    return _loads(data, cap, canon.jcs.MAX_DEPTH)


def loads_object(data: bytes, *, cap: int, keys: set[str] | frozenset[str]) -> dict[str, Any]:
    """:func:`loads_body` of an object with exactly ``keys``."""
    obj = loads_body(data, cap=cap)
    if type(obj) is not dict or set(obj) != set(keys):
        raise _bad("field set")
    return obj


def dumps_body(obj: Any, *, cap: int) -> bytes:
    """``cj(obj)``, refused locally if it is over ``cap`` (requests are built at or under the relay's caps)."""
    try:
        data = canon.dumps(obj)
    except canon.JcsError:
        raise _bad("not canonical JSON") from None
    if len(data) > cap:
        raise WireError("too_large", "request over cap")
    return data


def b64_field(value: object, *, max_len: int, exact: int | None = None) -> bytes:
    """Decode a canonical b64u field of at most ``max_len`` bytes; the text length is checked before decoding."""
    if type(value) is not str:
        raise _bad("b64u type")
    if len(value) > (max_len * 4 + 2) // 3:
        raise WireError("too_large", "b64u field over cap")
    try:
        out = crypto.unb64u(value, exact)
    except crypto.EncodingError:
        raise _bad("b64u") from None
    if len(out) > max_len:
        raise WireError("too_large", "b64u field over cap")
    return out


def ct_equal(a: bytes, b: bytes) -> bool:
    """Constant-time equality of two byte strings."""
    return hmac.compare_digest(a, b)


# --- signed objects ---------------------------------------------------------------------------------------------------

_SIGNED_KINDS = {"member_list": "sig_member_list", "genesis_pin": "sig_genesis_pin", "wk_grant": "sig_wk_grant"}


def verify_signed(pub: bytes, signed: object, kind: str) -> dict[str, Any] | None:
    """``o`` if ``signed`` is exactly ``{o, sig}`` with ``v: 2``, this suite and ``kind``, and ``sig`` verifies under
    ``pub`` over ``label(kind) || cj(o)`` (proto §2.4); ``None`` otherwise. The exact field set of ``o`` is the
    caller's check."""
    try:
        if type(signed) is not dict or set(signed) != {"o", "sig"}:
            return None
        o = signed["o"]
        if type(o) is not dict or o.get("v") != 2 or o.get("suite") != SUITE or o.get("kind") != kind:
            return None
        sig = crypto.unb64u(signed["sig"], SIG_LEN)
        return o if crypto.verify(pub, sig, L[_SIGNED_KINDS[kind]] + canon.dumps(o)) else None
    except (crypto.EncodingError, canon.JcsError, KeyError, TypeError):
        return None


# --- ORB2 -------------------------------------------------------------------------------------------------------------

_HEADER = struct.Struct(">4sBBBB16s16s16s16sQQI16s")
assert _HEADER.size == HEADER_LEN


@dataclass(frozen=True)
class Header:
    version: int
    direction: int
    flags: int
    suite: int
    workspace: bytes
    device: bytes
    rid: bytes
    stream: bytes
    seq: int
    ts_ms: int
    epoch: int
    salt: bytes

    def pack(self) -> bytes:
        for b in (self.workspace, self.device, self.rid, self.stream, self.salt):
            if type(b) is not bytes or len(b) != 16:
                raise _bad("header id")
        try:
            return _HEADER.pack(
                b"ORB2",
                self.version,
                self.direction,
                self.flags,
                self.suite,
                self.workspace,
                self.device,
                self.rid,
                self.stream,
                self.seq,
                self.ts_ms,
                self.epoch,
                self.salt,
            )
        except struct.error:
            raise _bad("header field range") from None


def parse_header(raw: bytes) -> Header:
    """The 108-byte ORB2 header (proto §8.1): magic, version 2, direction 1 or 2, this suite, the flags allowed for the
    direction (REFUSAL requires LAST), seq and ts_ms within the safe range."""
    if type(raw) is not bytes or len(raw) != HEADER_LEN:
        raise _bad("header length")
    magic, version, direction, flags, suite, ws, dev, rid, stream, seq, ts_ms, epoch, salt = _HEADER.unpack(raw)
    if magic != b"ORB2" or version != 2:
        raise _bad("magic or version")
    if suite != SUITE:
        raise WireError("wrong_suite", "suite")
    allowed = FLAG_STREAM if direction == 1 else (FLAG_LAST | FLAG_STREAM | FLAG_REFUSAL) if direction == 2 else None
    if allowed is None or flags & ~allowed or (flags & FLAG_REFUSAL and not flags & FLAG_LAST):
        raise _bad("direction or flags")
    if seq > MAX_SAFE_INT or ts_ms > MAX_SAFE_INT:
        raise _bad("seq or ts_ms")
    return Header(version, direction, flags, suite, ws, dev, rid, stream, seq, ts_ms, epoch, salt)


def _cap(direction: int) -> int:
    return MAX_REQUEST if direction == 1 else MAX_CHUNK


def parse_envelope(env: bytes, *, direction: int) -> tuple[Header, bytes, bytes]:
    """``(header, body, signature)`` of an envelope of ``direction``; ``body`` is ciphertext with its tag."""
    if type(env) is not bytes:
        raise _bad("envelope type")
    if len(env) > _cap(direction):
        raise WireError("too_large", "envelope over cap")
    if len(env) < OVERHEAD:
        raise _bad("envelope length")
    h = parse_header(env[:HEADER_LEN])
    if h.direction != direction:
        raise _bad("direction")
    return h, env[HEADER_LEN:-SIG_LEN], env[-SIG_LEN:]


def build_envelope(header: bytes, body: bytes, sig: bytes) -> bytes:
    h = parse_header(header)
    if type(body) is not bytes or len(body) < TAG_LEN or type(sig) is not bytes or len(sig) != SIG_LEN:
        raise _bad("body or signature")
    env = header + body + sig
    if len(env) > _cap(h.direction):
        raise WireError("too_large", "envelope over cap")
    return env


def bridge_signed_bytes(header: bytes, body: bytes) -> bytes:
    """``"orch/v2/sig/bridge|" || header || body``: what a device signs (direction 1) and the WSK signs (2)."""
    return L["sig_bridge"] + header + body


def _epoch_ok(epoch: int, *, low: int) -> int:
    if type(epoch) is not int or not low <= epoch < 2**32:
        raise _bad("epoch")
    return epoch


def k_bridge(wk: bytes, workspace: bytes, epoch: int) -> bytes:
    """``K_bridge(e)`` for an epoch ``e >= 1``."""
    if len(workspace) != 16 or len(wk) != 32:
        raise _bad("key or workspace length")
    info = f"orch/v2/bridge|{workspace.hex()}|{_epoch_ok(epoch, low=1)}".encode("ascii")
    return crypto.hkdf(wk, b"", info)


def k_pair(secret: bytes, workspace: bytes, offer: bytes) -> bytes:
    """``K_pair`` (epoch 0)."""
    if len(workspace) != 16 or len(offer) != 16:
        raise _bad("workspace or offer length")
    return crypto.hkdf(secret, b"", f"orch/v2/pair|{workspace.hex()}|{offer.hex()}".encode("ascii"))


def k_msg(channel_key: bytes, salt: bytes) -> bytes:
    if len(salt) != 16:
        raise _bad("salt length")
    return crypto.hkdf(channel_key, salt, L["kdf_bridge_msg"])


def seal_body(channel_key: bytes, header: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM under ``K_msg(channel_key, header.salt)`` with the header as AAD; returns ciphertext || tag."""
    h = parse_header(header)
    return crypto.aes_gcm_seal(k_msg(channel_key, h.salt), _ZERO_NONCE, plaintext, header)


def open_body(channel_key: bytes, header: bytes, body: bytes) -> bytes:
    h = parse_header(header)
    try:
        return crypto.aes_gcm_open(k_msg(channel_key, h.salt), _ZERO_NONCE, body, header)
    except (InvalidTag, ValueError):
        raise _bad("tag") from None


def build_plaintext(meta: Mapping[str, Any], data: bytes) -> bytes:
    """``u32(meta_len) || cj(meta) || data`` with ``meta_len <= 64 KiB``."""
    m = dumps_body(dict(meta), cap=MAX_META)
    return len(m).to_bytes(4, "big") + m + data


def parse_plaintext(pt: bytes) -> tuple[dict[str, Any], bytes]:
    if len(pt) < 4:
        raise _bad("plaintext length")
    n = int.from_bytes(pt[:4], "big")
    if n > MAX_META:
        raise WireError("too_large", "meta over cap")
    if 4 + n > len(pt):
        raise _bad("meta length")
    meta = loads_canonical(pt[4 : 4 + n], cap=MAX_META)
    if type(meta) is not dict:
        raise _bad("meta type")
    return meta, pt[4 + n :]


# --- staging sets (proto §19.8 P3-D20/P3-D37, contract 11A.5): pages, the manifest, the exact byte counter ------------
#
# A page is ``cj({kind, log, after_seq, from_acc, heads, digest})`` with ``digest = "sha256:" + hex(sha256(cj(page
# without digest)))`` (the relay's ``page_digest``, a transport convention, not an F1 hash domain). The manifest is
# ``cj({pages: [{i, kind, log, after_seq, count, digest}]})`` sorted by ``(kind, log, after_seq)`` (Python string order,
# as the relay compares it), with ``i`` the position in that order. The relay counts a set's staged bytes as the sum of
# the page bodies plus the manifest body (``checkpoint_staging.bytes``), so :func:`set_bytes` is that sum computed from
# the layout alone (every hash is 71 characters, so the size does not depend on the heads): R3's exact counter.

PAGE_CAP = 4 << 20  # a staging page or manifest body (the relay's PAGE_CAP)
PAGE_HEADS = (PAGE_CAP - 1024) // 74  # heads per page: 73 JSON bytes per head plus a separator, 1 KiB of framing
_KINDS_STAGED = ("appended", "abandoned", "retained")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_LOG = re.compile(r"workspace|[0-9A-HJKMNP-TV-Z]{26}")
_H0 = "sha256:" + "0" * 64


def check_hash(value: object) -> str:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise _bad("hash")
    return value


def _check_page_fields(kind: object, log: object, after_seq: object, count: object) -> None:
    if kind not in _KINDS_STAGED:
        raise _bad("page kind")
    if type(log) is not str or not _LOG.fullmatch(log):
        raise _bad("page log")
    if type(after_seq) is not int or not 0 <= after_seq <= MAX_SAFE_INT:
        raise _bad("page after_seq")
    if type(count) is not int or not 1 <= count <= PAGE_HEADS:
        raise _bad("page count")


def page_digest(page: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canon.dumps({k: v for k, v in page.items() if k != "digest"})).hexdigest()


def sha256_ref(data: bytes) -> str:
    """``"sha256:" + hex(sha256(data))``."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_page(
    kind: str, log: str, after_seq: int, from_acc: str, heads: list[str] | tuple[str, ...]
) -> tuple[bytes, str]:
    """The ``cj`` body of one staging page (at most :data:`PAGE_CAP`) and its digest (the manifest's)."""
    _check_page_fields(kind, log, after_seq, len(heads) if isinstance(heads, list | tuple) else None)
    check_hash(from_acc)
    for h in heads:
        check_hash(h)
    page: dict[str, Any] = {
        "kind": kind,
        "log": log,
        "after_seq": after_seq,
        "from_acc": from_acc,
        "heads": list(heads),
    }
    page["digest"] = digest = page_digest(page)
    data = canon.dumps(page)
    if len(data) > PAGE_CAP:
        raise WireError("too_large", "staging page over cap")
    return data, digest


def page_bytes(kind: str, log: str, after_seq: int, count: int) -> int:
    """``len(build_page(...))`` for a page of ``count`` heads, without the heads (each is 71 characters)."""
    _check_page_fields(kind, log, after_seq, count)
    one = {"kind": kind, "log": log, "after_seq": after_seq, "from_acc": _H0, "heads": [_H0], "digest": _H0}
    return len(canon.dumps(one)) + 74 * (count - 1)


@dataclass(frozen=True)
class PageSpec:
    """One page of a set's layout: its manifest index ``i`` and what it holds (the heads are read when it is built)."""

    i: int
    kind: str
    log: str
    after_seq: int
    count: int


def plan_pages(parts: list[tuple[str, str, int, int]] | tuple[tuple[str, str, int, int], ...]) -> list[PageSpec]:
    """The layout of a set: ``parts`` are ``(kind, log, after_seq, count)`` runs (``count`` >= 0; an empty run has no
    page), each split into pages of at most :data:`PAGE_HEADS` heads, sorted by ``(kind, log, after_seq)`` and
    indexed in that order. The runs of one ``(kind, log)`` must be contiguous (the relay refuses a gap or overlap)."""
    pages: list[tuple[str, str, int, int]] = []
    for kind, log, after_seq, count in parts:
        if type(count) is not int or count < 0:
            raise _bad("run count")
        done = 0
        while done < count:
            n = min(PAGE_HEADS, count - done)
            _check_page_fields(kind, log, after_seq + done, n)
            pages.append((kind, log, after_seq + done, n))
            done += n
    pages.sort(key=lambda p: (p[0], p[1], p[2]))
    for a, b in zip(pages, pages[1:], strict=False):
        if (a[0], a[1]) == (b[0], b[1]) and a[2] + a[3] != b[2]:
            raise _bad("runs of one kind and log are not contiguous")  # the relay's manifest rule
    return [PageSpec(i, *p) for i, p in enumerate(pages)]


def _descriptor(spec: PageSpec, digest: str) -> dict[str, Any]:
    return {"i": spec.i, "kind": spec.kind, "log": spec.log, "after_seq": spec.after_seq, "count": spec.count,
            "digest": digest}  # fmt: skip


def build_manifest(layout: list[PageSpec], digests: list[str]) -> bytes:
    """The ``cj`` manifest body of ``layout`` with the page digests (one per page, in index order)."""
    if len(digests) != len(layout) or any(s.i != k for k, s in enumerate(layout)):
        raise _bad("manifest layout")
    data = canon.dumps({"pages": [_descriptor(s, check_hash(d)) for s, d in zip(layout, digests, strict=True)]})
    if len(data) > PAGE_CAP:
        raise WireError("too_large", "manifest over cap")
    return data


def manifest_bytes(layout: list[PageSpec]) -> int:
    """``len(build_manifest(layout, ...))`` (every digest is 71 characters)."""
    if not layout:
        return len(canon.dumps({"pages": []}))
    one = len(canon.dumps({"pages": [_descriptor(layout[0], _H0)]}))
    return one + sum(len(canon.dumps(_descriptor(s, _H0))) + 1 for s in layout[1:])


def set_bytes(layout: list[PageSpec]) -> int:
    """The staged bytes the relay counts for this set: every page body plus the manifest body (P3-D37)."""
    return sum(page_bytes(s.kind, s.log, s.after_seq, s.count) for s in layout) + manifest_bytes(layout)
