"""Drop objects (orch-relay protocol-v2 §10 and §19.9; orch-v2-host 11A.16.5, D7; H13-0): the ORD2 blob, sealed
metadata, ``dek_commit``, the §10.3 wraps, the §10.4 descriptor, the §10.5 claim object, the same-DEK rewrap and the
ODU2 upload frame.

Pure functions; nothing here signs or reads a key from disk. The DEK is the caller's (32 random bytes per object
version); descriptors and claims are signed by the caller over :func:`drop_object_signed_bytes` and
:func:`drop_claim_signed_bytes` (``RelaySigner.drop_object`` / ``drop_claim``, H13-1).

Two levels of frame:

* :func:`encode_frame` / :func:`parse_frame` / :func:`reframe` are the **protocol** level: the relay's checks of
  §19.9 (the frame layout, the caps, the descriptor's field set, the inbox wrap rule, wraps that name this object and
  version, the blob that is the descriptor's). They reproduce every accepted vector frame byte-exact.
* :func:`build_host_frame` is the **host** level (D7): the author is this workspace, a ``file`` at version 1, never
  ``inbox``; a ``[space_id]`` frame carries exactly one ``wk`` wrap; a device-only frame carries ``device`` wraps to
  listed devices only, **never a ``wk``, ``wxk`` or ``sk`` wrap**.

Only a value that passes :func:`check_wrap_record` (a plain dict with exactly the six §10.3 fields) enters a frame at
either level, so the host-only :class:`orch.crypto.recovery.RecoveryEnvelope` (a typed value, and a different field set
in its storage form) can never be framed.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from orch import canon

from . import sealing, suite
from .encoding import EncodingError, b64u, unb64u, unhex
from .labels import LABELS, L

__all__ = [
    "BLOB_MAGIC",
    "DEFAULT_CHUNK",
    "DESCRIPTOR_FIELDS",
    "FRAME_MAGIC",
    "HEADER_LEN",
    "MAX_CHUNK",
    "MAX_HEAD",
    "MAX_META",
    "DropError",
    "build_host_frame",
    "check_drop_object",
    "check_wrap_record",
    "content_key",
    "decrypt_blob",
    "decrypt_chunk",
    "dek_commit",
    "drop_claim_object",
    "drop_claim_signed_bytes",
    "drop_object",
    "drop_object_signed_bytes",
    "encode_frame",
    "encrypt_blob",
    "open_meta",
    "open_wrap",
    "open_wrap_sealed",
    "open_wrap_wk",
    "parent_hash",
    "parse_blob_header",
    "parse_frame",
    "reframe",
    "rewrap_same_dek",
    "seal_meta",
    "verify_drop_object",
    "wrap_sealed",
    "wrap_wk",
]

BLOB_MAGIC = b"ORD2"
HEADER_LEN = 40
DEFAULT_CHUNK = 1 << 20
MAX_CHUNK = 64 << 20
FRAME_MAGIC = b"ODU2"
MAX_HEAD = 512 * 1024
MAX_DESCRIPTOR = 96 * 1024
MAX_META = 64 * 1024 + 16
MAX_WRAPS = 1024
MAX_WRAP_RECORD = 512
MAX_BLOB = 26 * 1024 * 1024  # the relay default ORCH_RELAY_DROP_MAX_BLOB_BYTES; the host's send cap is the same
MAX_SAFE_INT = (1 << 53) - 1
TAG = 16

DESCRIPTOR_FIELDS = frozenset(
    {
        "v", "suite", "kind", "object_id", "space_id", "space_kind", "object_kind", "version", "parent", "parent_hash",
        "author_kind", "author_id", "content_hash", "content_len", "dek_commit", "meta", "recipients", "origin",
        "created_ms", "expires_ms",
    }
)  # fmt: skip
WRAP_FIELDS = frozenset({"object_id", "version", "target_kind", "target_id", "key_version", "wrap"})
WRAP_KINDS = ("wk", "sk", "wxk", "device")
CLAIM_FIELDS = frozenset({"v", "suite", "kind", "object_id", "workspace_id", "wrap"})


class DropError(ValueError):
    """A malformed Drop input or object (never a decryption failure, which is ``InvalidTag``)."""


# --- small checks ---------------------------------------------------------------------------------------------------


def _b(value: object, n: int, what: str) -> bytes:
    if type(value) is not bytes or len(value) != n:
        raise DropError(f"{what} is {n} bytes")
    return value


def _u32(value: object, what: str, low: int = 0) -> int:
    if type(value) is not int or not low <= value < 2**32:
        raise DropError(f"{what} is a u32 >= {low}")
    return value


def _hexid(value: object, what: str) -> bytes:
    try:
        return unhex(value, 16)
    except EncodingError:
        raise DropError(f"{what} is a lower-case 32-hex id") from None


def _u32b(i: int) -> bytes:
    return i.to_bytes(4, "big")


# --- content (§10.2) ------------------------------------------------------------------------------------------------


def content_key(dek: bytes, object_id: bytes, version: int) -> bytes:
    info = f"{LABELS['kdf_drop_content']}|{_b(object_id, 16, 'object_id').hex()}|{_u32(version, 'version', 1)}"
    return suite.hkdf(_b(dek, 32, "DEK"), b"", info.encode("ascii"))


def _blob_header(object_id: bytes, version: int, chunk: int, prefix: bytes) -> bytes:
    if type(chunk) is not int or not 0 < chunk <= MAX_CHUNK:
        raise DropError("chunk size")
    return (
        BLOB_MAGIC
        + bytes([2, suite.SUITE_ID, 0, 0])
        + _b(object_id, 16, "object_id")
        + _u32b(_u32(version, "version", 1))
        + _u32b(chunk)
        + _b(prefix, 8, "nonce prefix")
    )


def parse_blob_header(header: bytes, object_id: bytes, version: int) -> tuple[int, bytes]:
    """``(chunk_size, nonce_prefix)`` of a 40-byte ORD2 header that names this suite, object and version."""
    if type(header) is not bytes or len(header) != HEADER_LEN:
        raise DropError("ORD2 header is 40 bytes")
    if header[:4] != BLOB_MAGIC or header[4] != 2 or header[5] != suite.SUITE_ID or header[6:8] != b"\0\0":
        raise DropError("not an ORD2 header of this suite")
    if header[8:24] != _b(object_id, 16, "object_id") or header[24:28] != _u32b(_u32(version, "version", 1)):
        raise DropError("the header names another object or version")
    chunk = int.from_bytes(header[28:32], "big")
    if not 0 < chunk <= MAX_CHUNK:
        raise DropError("chunk size")
    return chunk, header[32:40]


def _chunk_aad(header: bytes, i: int, is_last: bool) -> bytes:
    return header + _u32b(i) + bytes([1 if is_last else 0])


def _encrypt_blob_with_prefix(
    dek: bytes, object_id: bytes, version: int, pt: bytes, prefix: bytes, chunk: int = DEFAULT_CHUNK
) -> bytes:
    """Test seam (a fixed nonce prefix). Never call outside tests."""
    if type(pt) is not bytes:
        raise DropError("content is bytes")
    hdr = _blob_header(object_id, version, chunk, prefix)
    k = content_key(dek, object_id, version)
    n = max(1, -(-len(pt) // chunk))
    out = [hdr]
    for i in range(n):
        piece = pt[i * chunk : (i + 1) * chunk]
        out.append(suite.aes_gcm_seal(k, prefix + _u32b(i), piece, _chunk_aad(hdr, i, i == n - 1)))
    return b"".join(out)


def encrypt_blob(dek: bytes, object_id: bytes, version: int, pt: bytes, chunk: int = DEFAULT_CHUNK) -> bytes:
    """The ORD2 ciphertext of ``pt`` (empty content is one empty last chunk), with a fresh nonce prefix."""
    return _encrypt_blob_with_prefix(dek, object_id, version, pt, secrets.token_bytes(8), chunk)


def decrypt_chunk(k_content: bytes, header: bytes, i: int, ct: bytes, is_last: bool) -> bytes:
    """One chunk (streaming downloads, 11A.16.5): ``i`` and ``is_last`` are the reader's own count, never the data's."""
    if type(ct) is not bytes or len(ct) < TAG or type(i) is not int or not 0 <= i < 2**32:
        raise DropError("chunk")
    return suite.aes_gcm_open(k_content, header[32:40] + _u32b(i), ct, _chunk_aad(header, i, is_last))


def decrypt_blob(dek: bytes, object_id: bytes, version: int, blob: bytes) -> bytes:
    """The plaintext; a truncated, reordered, re-headed or substituted blob raises ``DropError`` or ``InvalidTag``."""
    if type(blob) is not bytes or len(blob) < HEADER_LEN + TAG:
        raise DropError("truncated blob")
    hdr = blob[:HEADER_LEN]
    chunk, _ = parse_blob_header(hdr, object_id, version)
    body, step = blob[HEADER_LEN:], chunk + TAG
    n = -(-len(body) // step)
    if len(body) - (n - 1) * step < TAG:
        raise DropError("truncated blob")
    k = content_key(dek, object_id, version)
    return b"".join(decrypt_chunk(k, hdr, i, body[i * step : (i + 1) * step], i == n - 1) for i in range(n))


# --- metadata, commitments, parent hash -----------------------------------------------------------------------------


def _meta_key_aad(dek: bytes, object_id: bytes, version: int) -> tuple[bytes, bytes]:
    obj, ver = _b(object_id, 16, "object_id"), _u32(version, "version", 1)
    k = suite.hkdf(_b(dek, 32, "DEK"), b"", f"{LABELS['kdf_drop_meta']}|{obj.hex()}|{ver}".encode("ascii"))
    return k, L["aad_drop_meta"] + bytes([suite.SUITE_ID]) + obj + _u32b(ver)


def seal_meta(dek: bytes, object_id: bytes, version: int, meta: dict) -> bytes:
    """``AES-256-GCM(HKDF(DEK, "", "orch/v2/drop-meta|obj|ver"), zero nonce, cj(meta), aad)`` (the DEK is per
    version, so the zero nonce is used once)."""
    k, aad = _meta_key_aad(dek, object_id, version)
    out = suite.aes_gcm_seal(k, suite.ZERO_NONCE, canon.dumps(meta), aad)
    if len(out) > MAX_META:
        raise DropError("meta above 64 KiB")
    return out


def open_meta(dek: bytes, object_id: bytes, version: int, sealed: bytes) -> dict:
    if type(sealed) is not bytes or not TAG <= len(sealed) <= MAX_META:
        raise DropError("meta size")
    k, aad = _meta_key_aad(dek, object_id, version)
    pt = suite.aes_gcm_open(k, suite.ZERO_NONCE, sealed, aad)
    try:
        meta = canon.loads_strict(pt)
    except (canon.JcsError, ValueError, UnicodeDecodeError):
        raise DropError("meta is not strict JSON") from None
    if type(meta) is not dict:
        raise DropError("meta is an object")
    return meta


def dek_commit(object_id: bytes, version: int, dek: bytes) -> bytes:
    return suite.sha256(
        L["h_dek_commit"] + _b(object_id, 16, "object_id") + _u32b(_u32(version, "version", 1)) + _b(dek, 32, "DEK")
    )


def parent_hash(parent_o: dict) -> bytes:
    """``H("orch/v2/drop-parent|" || cj(parent descriptor o))`` (§10.6); also the retire's ``expect_head``."""
    return suite.sha256(L["h_drop_parent"] + canon.dumps(parent_o))


# --- wraps (§10.3) --------------------------------------------------------------------------------------------------


def check_wrap_record(rec: object) -> dict:
    """``rec`` if it is a §10.3 wrap record: a plain dict of exactly the six fields, a known ``target_kind``, hex ids,
    u32 versions (``device`` at key version 0), a canonical b64u wrap, ``cj`` at most 512 bytes. Anything else (a
    recovery envelope, its storage form, a list, bytes) raises :class:`DropError`."""
    if type(rec) is not dict or set(rec) != WRAP_FIELDS:
        raise DropError("not a wrap record")
    if rec["target_kind"] not in WRAP_KINDS:
        raise DropError("target_kind")
    _hexid(rec["object_id"], "object_id")
    _hexid(rec["target_id"], "target_id")
    _u32(rec["version"], "version", 1)
    _u32(rec["key_version"], "key_version")
    if rec["target_kind"] == "device" and rec["key_version"] != 0:
        raise DropError("a device wrap has key_version 0")
    try:
        w = unb64u(rec["wrap"])
        size = len(canon.dumps(rec))
    except (EncodingError, canon.JcsError):
        raise DropError("wrap encoding") from None
    if size > MAX_WRAP_RECORD or len(w) < TAG:
        raise DropError("wrap size")
    return rec


def _wrap_aad(object_id: bytes, version: int, kind: str, target: bytes, key_version: int) -> bytes:
    t = kind.encode("ascii")
    return (
        L["aad_wrap"] + bytes([suite.SUITE_ID, len(t)]) + t + target + object_id + _u32b(version) + _u32b(key_version)
    )


def _k_wrap_wk(wk: bytes, ws: bytes, epoch: int) -> bytes:
    info = f"{LABELS['kdf_wrap_wk']}|{_b(ws, 16, 'workspace id').hex()}|{_u32(epoch, 'epoch')}"
    return suite.hkdf(_b(wk, 32, "WK"), b"", info.encode("ascii"))


def _record(object_id: bytes, version: int, kind: str, target: bytes, key_version: int, w: bytes) -> dict:
    return {
        "object_id": object_id.hex(),
        "version": version,
        "target_kind": kind,
        "target_id": target.hex(),
        "key_version": key_version,
        "wrap": b64u(w),
    }


def _wrap_wk_with_salt(
    wk: bytes, ws: bytes, epoch: int, object_id: bytes, version: int, dek: bytes, salt: bytes
) -> dict:
    """Test seam (a fixed salt). Never call outside tests."""
    obj, ver = _b(object_id, 16, "object_id"), _u32(version, "version", 1)
    aad = _wrap_aad(obj, ver, "wk", _b(ws, 16, "workspace id"), _u32(epoch, "epoch"))
    w = suite._salted_seal(_k_wrap_wk(wk, ws, epoch), L["kdf_wrap_msg"], aad, _b(dek, 32, "DEK"), salt)
    return _record(obj, ver, "wk", ws, epoch, w)


def wrap_wk(wk: bytes, ws: bytes, epoch: int, object_id: bytes, version: int, dek: bytes) -> dict:
    """A ``wk`` wrap record under ``WK_epoch`` of ``ws`` (SALTED-AEAD, a fresh salt drawn here)."""
    return _wrap_wk_with_salt(wk, ws, epoch, object_id, version, dek, secrets.token_bytes(16))


def _sealed_extra(kind: str, version: int) -> bytes:
    return bytes([1 if kind == "wxk" else 2]) + _u32b(version)


def _wrap_sealed_with_ephemeral(
    eph: EllipticCurvePrivateKey,
    kind: str,
    kx_pub: bytes,
    target: bytes,
    key_version: int,
    object_id: bytes,
    version: int,
    dek: bytes,
) -> dict:
    """Test seam (a fixed ephemeral key). Never call outside tests."""
    if kind not in ("wxk", "device"):
        raise DropError("a sealed wrap is wxk or device")
    obj, ver, kv = _b(object_id, 16, "object_id"), _u32(version, "version", 1), _u32(key_version, "key_version")
    if kind == "device" and kv != 0:
        raise DropError("a device wrap has key_version 0")
    w = sealing._seal_with_ephemeral(
        eph, kx_pub, _b(target, 16, "target id"), "drop-dek", obj, kv, _b(dek, 32, "DEK"), _sealed_extra(kind, ver)
    )
    return _record(obj, ver, kind, target, kv, w)


def wrap_sealed(
    kind: str, kx_pub: bytes, target: bytes, key_version: int, object_id: bytes, version: int, dek: bytes
) -> dict:
    """A ``wxk`` (to ``WXK_v`` of workspace ``target``, ``key_version`` = ``wxk_version``) or ``device`` (to the
    device's ``dk_kx``, ``key_version`` 0) wrap record, sealed with a fresh ephemeral key (§5.3, purpose
    ``drop-dek``)."""
    return _wrap_sealed_with_ephemeral(
        suite.generate_private_key(), kind, kx_pub, target, key_version, object_id, version, dek
    )


def open_wrap_wk(rec: dict, wk: bytes, ws: bytes) -> bytes:
    """The DEK of a ``wk`` record for workspace ``ws``, opened with ``WK`` of the record's ``key_version`` (the
    caller picks the epoch's key; that it is held and not burned is ``BridgeKeys``')."""
    rec = check_wrap_record(rec)
    if rec["target_kind"] != "wk" or rec["target_id"] != _b(ws, 16, "workspace id").hex():
        raise DropError("not a wk wrap of this workspace")
    obj, ver, kv = bytes.fromhex(rec["object_id"]), rec["version"], rec["key_version"]
    aad = _wrap_aad(obj, ver, "wk", ws, kv)
    dek = suite.salted_open(_k_wrap_wk(wk, ws, kv), L["kdf_wrap_msg"], aad, unb64u(rec["wrap"]))
    if len(dek) != 32:
        raise InvalidTag()
    return dek


def open_wrap_sealed(rec: dict, kx_priv: EllipticCurvePrivateKey) -> bytes:
    """The DEK of a ``wxk`` or ``device`` record, opened with the recipient's exchange key."""
    rec = check_wrap_record(rec)
    kind = rec["target_kind"]
    if kind not in ("wxk", "device"):
        raise DropError("not a sealed wrap")
    obj, ver, kv = bytes.fromhex(rec["object_id"]), rec["version"], rec["key_version"]
    target = bytes.fromhex(rec["target_id"])
    dek = sealing.open_sealed(kx_priv, target, "drop-dek", obj, kv, unb64u(rec["wrap"]), _sealed_extra(kind, ver))
    if len(dek) != 32:
        raise InvalidTag()
    return dek


def open_wrap(rec: dict, o: dict, *, wk: bytes | None = None, kx_priv: EllipticCurvePrivateKey | None = None) -> bytes:
    """The DEK of ``rec`` for descriptor object ``o`` (§10.4 "Readers MUST" step 3): the record names ``o``'s object
    and version, and the DEK matches ``o``'s ``dek_commit``. ``wk`` opens a ``wk`` record of ``o``'s space,
    ``kx_priv`` a ``wxk`` or ``device`` record."""
    rec = check_wrap_record(rec)
    o = check_drop_object(o)
    if rec["object_id"] != o["object_id"] or rec["version"] != o["version"]:
        raise DropError("the wrap names another object or version")
    if rec["target_kind"] == "wk":
        if wk is None:
            raise DropError("a wk wrap needs WK")
        dek = open_wrap_wk(rec, wk, bytes.fromhex(o["space_id"]))
    elif rec["target_kind"] in ("wxk", "device"):
        if kx_priv is None:
            raise DropError("a sealed wrap needs the recipient key")
        dek = open_wrap_sealed(rec, kx_priv)
    else:
        raise DropError("sk wraps are not opened here (shared spaces are P6)")
    if not _ct_eq(b64u(dek_commit(bytes.fromhex(o["object_id"]), o["version"], dek)), o["dek_commit"]):
        raise DropError("dek_commit mismatch")
    return dek


def _ct_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode()) if type(b) is str else False


def _rewrap_same_dek_with_salt(o: dict, dek: bytes, wk: bytes, epoch: int, salt: bytes) -> dict:
    """Test seam (a fixed salt). Never call outside tests."""
    o = check_drop_object(o)
    r = o["recipients"]
    if not (r == "inbox" or r == [o["space_id"]]):
        raise DropError("only a [space_id] or inbox object gets a wk wrap (never a device-only one)")
    obj = bytes.fromhex(o["object_id"])
    if not _ct_eq(b64u(dek_commit(obj, o["version"], _b(dek, 32, "DEK"))), o["dek_commit"]):
        raise DropError("not this version's DEK (the same-DEK rule)")
    return _wrap_wk_with_salt(wk, bytes.fromhex(o["space_id"]), epoch, obj, o["version"], dek, salt)


def rewrap_same_dek(o: dict, dek: bytes, wk: bytes, epoch: int) -> dict:
    """A new ``wk`` wrap of the **same** DEK at ``epoch`` (``stale_epoch``, ``drop_rewrap``, a claim): the DEK must be
    the one ``o`` commits to, and ``o`` is a ``[space_id]`` or inbox object, never a device-only one."""
    return _rewrap_same_dek_with_salt(o, dek, wk, epoch, secrets.token_bytes(16))


# --- the descriptor (§10.4) and the claim (§10.5) -------------------------------------------------------------------


def check_drop_object(o: object) -> dict:
    """``o`` if it is a well-formed §10.4 descriptor object of this suite (the relay's structural checks)."""
    if type(o) is not dict or set(o) != DESCRIPTOR_FIELDS:
        raise DropError("descriptor field set")
    if o["v"] != 2 or o["suite"] != suite.SUITE_ID or o["kind"] != "drop_object":
        raise DropError("descriptor header")
    _hexid(o["object_id"], "object_id")
    _hexid(o["space_id"], "space_id")
    _hexid(o["author_id"], "author_id")
    if o["space_kind"] != "workspace":
        raise DropError("space_kind (personal and shared are P6)")
    if o["object_kind"] not in ("file", "document") or o["origin"] not in ("human", "agent"):
        raise DropError("object_kind or origin")
    if o["author_kind"] not in ("device", "workspace"):
        raise DropError("author_kind")
    if o["author_kind"] == "workspace" and o["author_id"] != o["space_id"]:
        raise DropError("a workspace author is the space")
    ver = _u32(o["version"], "version", 1)
    if type(o["parent"]) is not int or o["parent"] != ver - 1:
        raise DropError("parent")
    if o["object_kind"] == "file" and ver != 1:
        raise DropError("a file has version 1 only")
    ph = o["parent_hash"]
    if (ver == 1) != (ph is None):
        raise DropError("parent_hash")
    try:
        if ph is not None:
            unb64u(ph, 32)
        unb64u(o["content_hash"], 32)
        unb64u(o["dek_commit"], 32)
        meta = unb64u(o["meta"])
    except EncodingError:
        raise DropError("descriptor encodings") from None
    if not TAG <= len(meta) <= MAX_META:
        raise DropError("meta size")
    cl = o["content_len"]
    if type(cl) is not int or not HEADER_LEN + TAG <= cl <= MAX_SAFE_INT:
        raise DropError("content_len")
    r = o["recipients"]
    if r == "inbox":
        if o["object_kind"] != "file":
            raise DropError("inbox is for files only")
    elif type(r) is not list or not r or r != sorted(set(r)) or len(r) > MAX_WRAPS:
        raise DropError("recipients is inbox or a sorted unique non-empty list")
    else:
        for t in r:
            _hexid(t, "recipient")
    for f in ("created_ms",):
        if type(o[f]) is not int or not 0 <= o[f] <= MAX_SAFE_INT:
            raise DropError(f)
    exp = o["expires_ms"]
    if o["object_kind"] == "file":
        if type(exp) is not int or not o["created_ms"] < exp <= MAX_SAFE_INT:
            raise DropError("a file expires")
    elif exp is not None:
        raise DropError("a document has no expiry")
    try:
        size = len(canon.dumps(o))
    except canon.JcsError:
        raise DropError("descriptor is not canonical JSON") from None
    if size > MAX_DESCRIPTOR:
        raise DropError("descriptor above 96 KiB")
    return o


def drop_object(
    *,
    object_id: bytes,
    space_id: bytes,
    object_kind: str,
    version: int,
    parent_hash: bytes | None,
    author_kind: str,
    author_id: bytes,
    blob: bytes,
    dek: bytes,
    meta: bytes,
    recipients: str | Iterable[bytes | str],
    origin: str,
    created_ms: int,
    expires_ms: int | None,
) -> dict:
    """The §10.4 descriptor object for an ORD2 ``blob`` and sealed ``meta`` under ``dek`` (``content_hash``,
    ``content_len`` and ``dek_commit`` computed here), checked by :func:`check_drop_object`."""
    obj = _b(object_id, 16, "object_id")
    if type(blob) is not bytes or type(meta) is not bytes:
        raise DropError("blob and meta are bytes")
    parse_blob_header(blob[:HEADER_LEN], obj, version)
    if recipients != "inbox":
        if isinstance(recipients, str):
            raise DropError("recipients")
        ids = [r.hex() if type(r) is bytes else r for r in recipients]
        recipients = ids
    o = {
        "v": 2,
        "suite": suite.SUITE_ID,
        "kind": "drop_object",
        "object_id": obj.hex(),
        "space_id": _b(space_id, 16, "space_id").hex(),
        "space_kind": "workspace",
        "object_kind": object_kind,
        "version": version,
        "parent": version - 1 if type(version) is int else version,
        "parent_hash": None if parent_hash is None else b64u(_b(parent_hash, 32, "parent_hash")),
        "author_kind": author_kind,
        "author_id": _b(author_id, 16, "author_id").hex(),
        "content_hash": b64u(suite.sha256(blob)),
        "content_len": len(blob),
        "dek_commit": b64u(dek_commit(obj, version, dek)),
        "meta": b64u(meta),
        "recipients": recipients,
        "origin": origin,
        "created_ms": created_ms,
        "expires_ms": expires_ms,
    }
    return check_drop_object(o)


def drop_object_signed_bytes(o: dict) -> bytes:
    """What the author signs (``sig_drop_object``): ``RelaySigner.drop_object`` for the host (H13-1)."""
    return L["sig_drop_object"] + canon.dumps(check_drop_object(o))


def verify_drop_object(signed: object, author_pub: bytes) -> dict | None:
    """The descriptor object of ``{"o", "sig"}`` if it is well formed and its signature verifies under
    ``author_pub``; else ``None`` (§10.4 "Readers MUST" step 1; the author's standing is the caller's)."""
    if type(signed) is not dict or set(signed) != {"o", "sig"} or type(signed["sig"]) is not str:
        return None
    try:
        o = check_drop_object(signed["o"])
        sig = unb64u(signed["sig"], suite.SIG_LEN)
    except (DropError, EncodingError):
        return None
    return o if suite.verify(author_pub, sig, L["sig_drop_object"] + canon.dumps(o)) else None


def drop_claim_object(object_id: bytes, workspace_id: bytes, wrap: dict) -> dict:
    """The §10.5 claim object: a ``wk`` wrap of this object for the claiming workspace."""
    w = check_wrap_record(wrap)
    obj, ws = _b(object_id, 16, "object_id").hex(), _b(workspace_id, 16, "workspace id").hex()
    if w["target_kind"] != "wk" or w["target_id"] != ws or w["object_id"] != obj:
        raise DropError("a claim carries a wk wrap of this object for the claiming workspace")
    return {"v": 2, "suite": suite.SUITE_ID, "kind": "drop_claim", "object_id": obj, "workspace_id": ws, "wrap": w}


def drop_claim_signed_bytes(o: dict) -> bytes:
    """What ``WSK`` signs for a claim (``sig_drop_claim``)."""
    if type(o) is not dict or set(o) != CLAIM_FIELDS:
        raise DropError("claim field set")
    want = drop_claim_object(_hexid(o["object_id"], "object_id"), _hexid(o["workspace_id"], "workspace_id"), o["wrap"])
    if want != o:
        raise DropError("claim header")
    return L["sig_drop_claim"] + canon.dumps(o)


# --- the ODU2 upload frame (§19.9) ----------------------------------------------------------------------------------


def _check_frame(signed: object, wraps: object, blob: object) -> tuple[dict, list[dict]]:
    """The relay's frame checks that need no state: the descriptor, the wraps and the blob agree."""
    if type(signed) is not dict or set(signed) != {"o", "sig"} or type(signed["sig"]) is not str:
        raise DropError("descriptor is a signed object")
    o = check_drop_object(signed["o"])
    try:
        unb64u(signed["sig"], suite.SIG_LEN)
    except EncodingError:
        raise DropError("descriptor signature encoding") from None
    if type(wraps) is not list or len(wraps) > MAX_WRAPS:
        raise DropError("wraps is a list of at most 1,024")
    seen: set[tuple[str, str, int]] = set()
    for w in wraps:
        w = check_wrap_record(w)
        if w["object_id"] != o["object_id"] or w["version"] != o["version"]:
            raise DropError("a wrap names another object or version")
        if w["target_kind"] == "sk":
            raise DropError("sk wraps (shared spaces are P6)")
        if w["target_kind"] in ("wk", "wxk") and w["target_id"] != o["space_id"]:
            raise DropError("a wk or wxk wrap names another workspace")
        key = (w["target_kind"], w["target_id"], w["key_version"])
        if key in seen:
            raise DropError("duplicate wrap target")
        seen.add(key)
    if o["recipients"] == "inbox":
        kinds = sorted(w["target_kind"] for w in wraps)
        if kinds not in (["wxk"], ["device", "wxk"]):
            raise DropError("an inbox object carries one wxk wrap and optionally the author's device wrap")
        if any(w["target_kind"] == "device" and w["target_id"] != o["author_id"] for w in wraps):
            raise DropError("an inbox device wrap is the author's own")
    if type(blob) is not bytes or len(blob) != o["content_len"] or len(blob) > MAX_BLOB:
        raise DropError("blob length is content_len")
    parse_blob_header(blob[:HEADER_LEN], bytes.fromhex(o["object_id"]), o["version"])
    if b64u(suite.sha256(blob)) != o["content_hash"]:
        raise DropError("content_hash mismatch")
    return o, wraps


def encode_frame(signed: dict, wraps: list[dict], blob: bytes) -> bytes:
    """``"ODU2" || u32be(head_len) || cj({"descriptor", "wraps"}) || blob`` after the protocol checks."""
    _check_frame(signed, wraps, blob)
    head = canon.dumps({"descriptor": signed, "wraps": wraps})
    if len(head) > MAX_HEAD:
        raise DropError("head above 512 KiB")
    return FRAME_MAGIC + _u32b(len(head)) + head + blob


def parse_frame(body: bytes) -> tuple[dict, bytes]:
    """``({"descriptor", "wraps"}, blob)`` of an ODU2 frame, with the protocol checks; the head must be ``cj``
    (the host parses only frames it built)."""
    if type(body) is not bytes or len(body) < 8 or body[:4] != FRAME_MAGIC:
        raise DropError("not an ODU2 frame")
    head_len = int.from_bytes(body[4:8], "big")
    if head_len > MAX_HEAD:
        raise DropError("head above 512 KiB")
    if len(body) < 8 + head_len:
        raise DropError("truncated head")
    raw = body[8 : 8 + head_len]
    try:
        head = canon.loads_strict(raw)
    except (canon.JcsError, ValueError, UnicodeDecodeError):
        raise DropError("head is not strict JSON") from None
    if type(head) is not dict or set(head) != {"descriptor", "wraps"}:
        raise DropError("head is exactly {descriptor, wraps}")
    if canon.dumps(head) != raw:
        raise DropError("head is not canonical")
    blob = body[8 + head_len :]
    _check_frame(head["descriptor"], head["wraps"], blob)
    return head, blob


def reframe(body: bytes, wraps: list[dict]) -> bytes:
    """The same descriptor and blob with new wraps (the same-DEK rewrap after ``stale_epoch``; frame ``gen + 1``)."""
    head, blob = parse_frame(body)
    return encode_frame(head["descriptor"], wraps, blob)


def build_host_frame(signed: dict, wraps: list[dict], blob: bytes) -> bytes:
    """A frame the host writes (D7): the protocol checks, plus the host's own descriptor (author this workspace, a
    ``file`` at version 1, not ``inbox``) and its addressing: a ``[space_id]`` frame carries exactly one ``wk`` wrap;
    a device-only frame carries only ``device`` wraps to listed devices, never a workspace wrap."""
    o, wraps = _check_frame(signed, wraps, blob)
    if o["author_kind"] != "workspace" or o["object_kind"] != "file" or o["version"] != 1:
        raise DropError("the host writes files of its own workspace only")
    r = o["recipients"]
    if r == "inbox":
        raise DropError("the host never writes inbox objects")
    if r == [o["space_id"]]:
        if [w["target_kind"] for w in wraps] != ["wk"]:
            raise DropError("a [space_id] frame carries exactly one wk wrap")
    else:
        if o["space_id"] in r:
            raise DropError("recipients is [space_id] or a device-only list")
        if not wraps:
            raise DropError("a device-only frame needs device wraps")
        for w in wraps:
            if w["target_kind"] != "device":
                raise DropError("a device-only frame carries no workspace wrap (D7)")
            if w["target_id"] not in r:
                raise DropError("a device wrap to a device outside recipients")
    return encode_frame(signed, wraps, blob)
