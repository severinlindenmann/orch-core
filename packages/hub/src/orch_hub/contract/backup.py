"""Hub backup file (protocol.md §17, design M4 slice S0): header, MAC, seal, extraction rules, restore floor.

Pure functions, no I/O, no clock (``now_s`` is passed in), no key storage, no fixed-key seams: the sealing helper takes
the ephemeral key as an argument and the public entry draws a fresh one. The vectors build files with
``contracts/hub/make_vectors_hub_backup.py`` (test code) and every case is replayed by ``tests/test_hub_backup_vectors.py``.

A backup file is ``MAGIC || u32 len || cj(envelope) || sealed`` where ``envelope = {"o": header, "sig": b64u, "mac":
b64u}``. ``sig`` is ``HK_root`` over ``LABEL || cj(header)``; ``mac`` is ``HMAC-SHA256(K_auth, cj(header))`` with
``K_auth`` derived from the typed words; ``sealed`` is ``eph_pub || AES-256-GCM`` over a tar of the files the header lists,
sealed to ``bk_pub`` (the public key of the words), with the header as AAD. Every restore check (signature, hub id,
dev flag, key, MAC, file list, seal, tar entries, hashes) passes inside :func:`open_backup` before the caller writes a byte.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
import struct
import tarfile
import unicodedata
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import suite as S
from .canon import MAX_SAFE_INT, CanonError, b64u, cj, loads_strict, unb64u
from .hub import Refused, _dev, _envelope, _hex16, _int_min, _key_field, _pub, _signature, hub_id

KIND = "hub.backup"
#: ``HK_root`` signs the header under this label. Hub-local like ``ACCESS_CHANGE_LABEL`` (it is not in ``HUB_LABELS``, so the
#: H0 vector file does not list it); ``tests/test_hub_backup_vectors.py`` checks it is prefix-free with every label.
LABEL = b"orch/v2/hub-backup|"
AAD_LABEL = b"orch/v2/hub-backup-aad|"
KDF_BK_SALT = b"orch/v2/hub-backup-key|" + bytes([S.SUITE_ID])
KDF_AUTH_INFO = b"orch/v2/hub-backup-auth"
SEAL_PURPOSE = "hub-backup"
SCRYPT = {"n": 2**17, "r": 8, "p": 1, "dklen": 40}
MAGIC = b"ORCHBAK1"
MAX_HEADER = 1 << 20
MAX_TOTAL = 200 * 1024 * 1024  # bytes of all files together (also the upload limit)
MAX_FILES = 512
SEQ_JUMP = 1000
SEQ_FIELDS = ("op_seq", "card_seq", "snapshot_seq")
HEADER_FIELDS = frozenset(
    {"v", "suite", "kind", "hv", "hub_id", "hk_root_pub", "made_ms", *SEQ_FIELDS, "bk_pub", "files", "dev"}
)
REQUIRED_FILES = ("hk_root.key", "hxk.key", "meta.json")
#: Paths a backup never lists (design 4.2, Opus conditions 11 and 13). Rows of the stores that must not be copied
#: (sessions, pending TOTP, pairing codes, limiter rows, switch tickets, push tokens) are removed from the copies, not by path.
EXCLUDED_NAMES = frozenset({"setup-token", "route-queue.db", "event-deliver.db"})
EXCLUDED_DIRS = frozenset({"backups", "tmp", "logs", "state.new", "state.old"})
EXCLUDED_SUFFIXES = (".log", ".tmp", "-wal", "-shm", "-journal", ".lock", ".pid")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _invalid(reason: str) -> Refused:
    return Refused("backup_invalid", reason=reason)


# --- the words: BK, bk_pub, K_auth ------------------------------------------------------------------------------------


def normalise_words(words: str) -> str:
    """NFKD, lower case, split on white space, rejoined with single spaces (as core ``identity/recovery``)."""
    return " ".join(unicodedata.normalize("NFKD", words).lower().split())


@dataclass(frozen=True)
class BackupKeys:
    """What the 24 words give: the private key (for opening the seal), its public key, and the MAC key."""

    bk: ec.EllipticCurvePrivateKey
    bk_pub: bytes
    k_auth: bytes


def derive_keys(words: str) -> BackupKeys:
    """``okm = scrypt(normalised words, salt = "orch/v2/hub-backup-key|" || suite, N=2^17, r=8, p=1, dklen=40)``;
    ``BK = (int(okm) mod (n-1)) + 1`` (FIPS 186-5 A.2.1); ``K_auth = HKDF-SHA256(okm, salt = 32 zero bytes, info =
    "orch/v2/hub-backup-auth")``. Exactly 24 words (the BIP-39 wordlist and checksum are checked by the caller before
    this; a mistyped list of 24 other words simply gives another key). Parameters are part of the format."""
    text = normalise_words(words)
    if len(text.split(" ")) != 24:
        raise _invalid("words")
    okm = Scrypt(salt=KDF_BK_SALT, length=SCRYPT["dklen"], n=SCRYPT["n"], r=SCRYPT["r"], p=SCRYPT["p"]).derive(
        text.encode("utf-8")
    )
    bk = ec.derive_private_key(int.from_bytes(okm, "big") % (S.P256_N - 1) + 1, ec.SECP256R1())
    k_auth = HKDF(algorithm=hashes.SHA256(), length=32, salt=bytes(32), info=KDF_AUTH_INFO).derive(okm)
    return BackupKeys(bk, S.public_bytes(bk), k_auth)


def header_mac(k_auth: bytes, header: Any) -> str:
    """``mac = HMAC-SHA256(K_auth, cj(header))``, base64url."""
    return b64u(hmac.new(k_auth, cj(header), hashlib.sha256).digest())


# --- the file list ----------------------------------------------------------------------------------------------------


def valid_path(path: object) -> bool:
    """Relative, ``/``-separated, 1-8 segments of ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}`` (so never absolute, never empty,
    never ``.`` or ``..``, never a backslash), at most 200 characters."""
    if type(path) is not str or not 1 <= len(path) <= 200:
        return False
    segs = path.split("/")
    return len(segs) <= 8 and all(_SEGMENT.fullmatch(s) and s not in (".", "..") for s in segs)


def is_excluded(path: str) -> bool:
    segs = path.split("/")
    return (
        segs[0] in EXCLUDED_DIRS
        or segs[-1] in EXCLUDED_NAMES
        or any(segs[-1].endswith(x) for x in EXCLUDED_SUFFIXES)
        or segs[-1].startswith(("route-queue.db", "event-deliver.db"))
    )


def check_file_list(files: Any) -> list[dict[str, Any]]:
    """The header's ``files``: 1-512 entries ``{path, size, sha256}`` (exact keys), paths valid, not excluded, strictly
    ascending (so unique), none a directory of another and none equal to another when case-folded, the required files present, each size and the sum ≤ 200 MiB."""
    if type(files) is not list or not 1 <= len(files) <= MAX_FILES:
        raise _invalid("files")
    total, prev = 0, ""
    dirs: set[str] = set()  # every directory prefix of the paths so far
    names: set[str] = set()  # the case-folded paths so far
    for f in files:
        if type(f) is not dict or set(f) != {"path", "size", "sha256"}:
            raise _invalid("files")
        p, size, digest = f["path"], f["size"], f["sha256"]
        if not valid_path(p) or is_excluded(p):
            raise _invalid("files_path")
        if not (type(size) is int and 0 <= size <= MAX_TOTAL) or not (
            type(digest) is str and _SHA256.fullmatch(digest)
        ):
            raise _invalid("files_size")
        if p <= prev:
            raise _invalid("files_order")
        folded = p.casefold()
        segs = folded.split("/")
        prefixes = {"/".join(segs[:i]) for i in range(1, len(segs))}
        if folded in names or folded in dirs or not prefixes.isdisjoint(names):
            raise _invalid("files_order")
        names.add(folded)
        dirs |= prefixes
        prev = p
        total += size
    if total > MAX_TOTAL:
        raise _invalid("files_size")
    if not set(REQUIRED_FILES) <= {f["path"] for f in files}:
        raise _invalid("files_required")
    return files


def check_tar(tar: bytes, files: list[dict[str, Any]]) -> dict[str, bytes]:
    """Extract in memory exactly the listed files: every tar entry must be a regular file (type ``0``; no symlink, hard
    link, device, fifo, directory, sparse or pax-only trick), have a safe relative name that is in the list, appear once,
    have the listed size (checked from the entry header before any data is read) and the listed SHA-256; every listed file
    must appear. Returns ``{path: bytes}``; the caller writes them under its own staging dir with its own modes (the
    tar's modes, owners and times are ignored)."""
    want = {f["path"]: f for f in files}
    got: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tar), mode="r:") as tf:
            for ti in tf:
                if ti.type != tarfile.REGTYPE:
                    raise _invalid("tar_type")
                name = ti.name
                if name.startswith("/") or "\\" in name or ".." in name.split("/") or not valid_path(name):
                    raise _invalid("tar_path")
                spec = want.get(name)
                if spec is None:
                    raise _invalid("tar_unlisted")
                if name in got:
                    raise _invalid("tar_dup")
                if ti.size != spec["size"]:
                    raise _invalid("tar_size")
                fh = tf.extractfile(ti)
                data = fh.read() if fh is not None else b""
                if len(data) != spec["size"] or hashlib.sha256(data).hexdigest() != spec["sha256"]:
                    raise _invalid("hash")
                got[name] = data
    except tarfile.TarError, EOFError, ValueError, OSError:
        raise _invalid("tar") from None
    if len(got) != len(want):
        raise _invalid("tar_missing")
    return got


# --- the sealed payload -----------------------------------------------------------------------------------------------


def _aad(header: Any) -> bytes:
    return AAD_LABEL + cj(header)


def _seal_key(ss: bytes, eph_pub: bytes, bk_pub: bytes, hub: str) -> bytes:
    info = b"orch/v2/seal|" + SEAL_PURPOSE.encode() + b"|" + hub.encode()
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + bk_pub, info=info).derive(ss)


def _seal_with(eph: ec.EllipticCurvePrivateKey, bk_pub: bytes, header: dict[str, Any], tar: bytes) -> bytes:
    eph_pub = S.public_bytes(eph)
    ss = eph.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bk_pub))
    key = _seal_key(ss, eph_pub, bk_pub, header["hub_id"])
    return eph_pub + AESGCM(key).encrypt(bytes(12), tar, _aad(header))


def seal_payload(bk_pub: bytes, header: dict[str, Any], tar: bytes) -> bytes:
    """``eph_pub (65) || AES-256-GCM(K, 0-nonce, tar, AAD)`` with a fresh ephemeral key per seal, ``K = HKDF(ECDH,
    salt = eph_pub || bk_pub, info = "orch/v2/seal|hub-backup|" || hub_id)``, ``AAD = "orch/v2/hub-backup-aad|" ||
    cj(header)``. The 0-nonce is safe because every seal has its own key."""
    return _seal_with(ec.generate_private_key(ec.SECP256R1()), bk_pub, header, tar)


def open_payload(bk: ec.EllipticCurvePrivateKey, header: dict[str, Any], sealed: bytes) -> bytes:
    if len(sealed) < 65 + 16 or len(sealed) > MAX_TOTAL + (4 << 20):
        raise _invalid("seal")
    eph_pub, ct = sealed[:65], sealed[65:]
    if not S.valid_public_key(eph_pub):
        raise _invalid("seal")
    ss = bk.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), eph_pub))
    if ss == bytes(32):
        raise _invalid("seal")
    key = _seal_key(ss, eph_pub, S.public_bytes(bk), header["hub_id"])
    try:
        return AESGCM(key).decrypt(bytes(12), ct, _aad(header))
    except InvalidTag:
        raise _invalid("seal") from None


# --- the file ----------------------------------------------------------------------------------------------------------


def pack_file(envelope: dict[str, Any], sealed: bytes) -> bytes:
    head = cj(envelope)
    return MAGIC + struct.pack(">I", len(head)) + head + sealed


def unpack_file(data: bytes) -> tuple[dict[str, Any], bytes]:
    """Split a file into its envelope (strict canonical JSON, byte for byte ``cj``) and the sealed payload."""
    if type(data) is not bytes or len(data) < len(MAGIC) + 4 or data[: len(MAGIC)] != MAGIC:
        raise _invalid("container")
    (n,) = struct.unpack(">I", data[len(MAGIC) : len(MAGIC) + 4])
    start = len(MAGIC) + 4
    if not 2 <= n <= MAX_HEADER or start + n > len(data):
        raise _invalid("container")
    raw = data[start : start + n]
    try:
        env = loads_strict(raw)
        if cj(env) != raw:
            raise _invalid("container")
    except CanonError:
        raise _invalid("container") from None
    return env, data[start + n :]


@dataclass(frozen=True)
class Opened:
    header: dict[str, Any]
    files: dict[str, bytes]


def read_header(
    data: bytes, *, dev_mode: bool, live: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """The checks that need no words, in this order: container, envelope, the root key field, ``HK_root`` signature over the
    header, exact header fields, the file list, ``hub_id`` = id of ``hk_root_pub``, the dev flag, and (in place) that this is
    the running hub. A fresh-server restore shows the fingerprint of ``header["hub_id"]`` after this and before asking for
    the words. Returns ``(envelope, header, sealed)``."""
    env, sealed = unpack_file(data)
    try:
        o = _envelope(env, KIND, ("mac",))
        root = _key_field(o, "hk_root_pub")
    except Refused:
        raise _invalid("envelope") from None
    try:
        _signature(env, LABEL, root)
    except Refused:
        raise _invalid("sig") from None
    if set(o) != HEADER_FIELDS:
        raise _invalid("fields")
    ok = (
        _hex16(o["hub_id"])
        and _pub(o["bk_pub"])
        and type(o["dev"]) is bool
        and type(o["made_ms"]) is int
        and all(_int_min(0)(o[k]) and o[k] <= MAX_SAFE_INT for k in ("made_ms", *SEQ_FIELDS))
    )
    if not ok:
        raise _invalid("fields")
    try:
        unb64u(env["mac"], 32)
    except CanonError:
        raise _invalid("envelope") from None
    check_file_list(o["files"])
    if o["hub_id"] != hub_id(root):
        raise _invalid("hub_id")
    try:
        _dev(o, dev_mode)
    except Refused as e:
        raise Refused(e.code) from None
    if live is not None and (o["hub_id"] != live["hub_id"] or o["hk_root_pub"] != live["hk_root_pub"]):
        raise Refused("other_hub")
    return env, o, sealed


def open_backup(data: bytes, *, words: str, dev_mode: bool, live: dict[str, Any] | None) -> Opened:
    """Every restore check, nothing written. After :func:`read_header`: derive the keys from the typed words, ``bk_pub``
    must equal the header's (else ``wrong_backup_key``), the MAC must match (else ``backup_invalid``/``mac``: this is what
    stops a backup forged under another root and sealed to a copied ``bk_pub``), open the seal with the header as AAD, then
    :func:`check_tar`. ``live`` = ``{"hub_id", "hk_root_pub"}`` of the running hub for an in-place restore, ``None`` on a
    fresh server."""
    env, o, sealed = read_header(data, dev_mode=dev_mode, live=live)
    keys = derive_keys(words)
    if not hmac.compare_digest(keys.bk_pub, unb64u(o["bk_pub"], 65)):
        raise Refused("wrong_backup_key")
    if not hmac.compare_digest(unb64u(env["mac"], 32), unb64u(header_mac(keys.k_auth, o), 32)):
        raise _invalid("mac")
    return Opened(o, check_tar(open_payload(keys.bk, o, sealed), o["files"]))


# --- anti-rollback: the sequence floor --------------------------------------------------------------------------------


def restore_floor(live: dict[str, int] | None, backup: dict[str, int], now_s: int) -> dict[str, int]:
    """The new ``op_seq``, ``card_seq`` and ``snapshot_seq`` after a restore, each above everything ever issued:
    in place ``max(live, backup) + 1000`` (a second restore of the same backup climbs again), on a fresh server
    ``max(backup + 1000, unix seconds)``. Every result must stay below 2^53 (else ``backup_invalid``/``seq_overflow``)."""
    out: dict[str, int] = {}
    for k in SEQ_FIELDS:
        b = backup[k]
        if type(b) is not int or not 0 <= b <= MAX_SAFE_INT:
            raise _invalid("fields")
        if live is None:
            v = max(b + SEQ_JUMP, now_s)
        else:
            if type(live[k]) is not int or not 0 <= live[k] <= MAX_SAFE_INT:
                raise _invalid("fields")
            v = max(live[k], b) + SEQ_JUMP
        if v > MAX_SAFE_INT:
            raise _invalid("seq_overflow")
        out[k] = v
    return out
