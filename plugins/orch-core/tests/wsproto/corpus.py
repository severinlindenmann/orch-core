"""The vendored orch-relay corpus (11A.10): ``vectors_v2.json`` and ``http_fixtures/`` at the commit named in
``SOURCE``. Loader helpers only; the drift guard is ``test_corpus.py``.

The vector file is stored **xz-compressed** (``vectors_v2.json.xz``, ~0.4 MB instead of 23 MB, round 6 orch-52 #6), so
git history does not grow by the raw file at each corpus bump. ``SOURCE``'s ``vectors_v2_sha256`` is over the
**decompressed** bytes, which are the relay's ``tests/vectors_v2.json`` byte for byte. This module is the only reader:
every test that needs the vectors (``tests/crypto``, ``tests/canon``, ``tests/test_skeleton.py``) loads them here."""

from __future__ import annotations

import base64
import hashlib
import json
import lzma
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from orch.crypto import P256_N

DIR = Path(__file__).resolve().parent / "vectors"
FIXTURES = DIR / "http_fixtures"
VECTORS_PATH = DIR / "vectors_v2.json.xz"


MAX_VECTORS_BYTES = 64 << 20  # the decompressed bound (the file is ~23 MB): a decompression bomb stops here


class CorpusError(Exception):
    """The vendored vector file is not the pinned one (sha256 over the decompressed bytes), truncated, or too large."""


def source() -> dict[str, str]:
    out = {}
    for line in (DIR / "SOURCE").read_text().splitlines():
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def decompress(raw: bytes, *, limit: int = MAX_VECTORS_BYTES) -> bytes:
    """One complete xz stream, at most ``limit`` bytes decompressed (never more is produced), no trailing data."""
    d = lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    try:
        out = d.decompress(raw, max_length=limit + 1)
    except lzma.LZMAError as e:
        raise CorpusError(f"vectors_v2.json.xz is not a valid xz stream ({e})") from None
    if len(out) > limit:
        raise CorpusError(f"vectors_v2.json.xz decompresses to more than {limit} bytes")
    if not d.eof or d.unused_data:
        raise CorpusError("vectors_v2.json.xz is truncated or carries trailing data")
    return out


def vectors_bytes() -> bytes:
    """The decompressed ``vectors_v2.json``, checked against ``SOURCE``'s pin at every load (round 7): a tampered file
    fails here, before any test uses a vector, not only in ``test_corpus``."""
    out = decompress(VECTORS_PATH.read_bytes())
    if hashlib.sha256(out).hexdigest() != source()["vectors_v2_sha256"]:
        raise CorpusError("vectors_v2.json.xz does not match SOURCE's vectors_v2_sha256 (decompressed bytes)")
    return out


VECTORS = json.loads(vectors_bytes())
S2 = VECTORS["suites"]["2"]
KEYS = S2["keys"]
IDS = S2["ids"]


def priv(name: str):
    """The vector key ``name`` (seed mapping ``seed mod (n-1) + 1``, test-only)."""
    d = int.from_bytes(bytes.fromhex(KEYS[name]["seed"]), "big") % (P256_N - 1) + 1
    return ec.derive_private_key(d, ec.SECP256R1())


def pub(name: str) -> bytes:
    return bytes.fromhex(KEYS[name]["pub"])


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def case(name: str, case_name: str) -> dict[str, Any]:
    for c in fixture(name)["cases"]:
        if c["name"] == case_name:
            return c
    raise KeyError(f"{name}:{case_name}")


def request_body(req: dict[str, Any]) -> bytes | None:
    """The exact request bytes of a fixture case (README: ``cj(json)``, decoded ``body_b64u`` or ``body_recipe``)."""
    from orch import canon

    if "json" in req:
        return canon.dumps(req["json"])
    if "body_b64u" in req:
        return _b64(req["body_b64u"])
    if "body_recipe" in req:
        r = req["body_recipe"]
        return _b64(r["prefix_b64u"]) + bytes([r["byte"]]) * r["count"] + _b64(r["suffix_b64u"])
    return None


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
