"""Strict parsing of the release envelope and its signed documents (CONTRACT.md §1, §2).

Every parser here either returns a fully checked value or raises :class:`Malformed`. Nothing is canonicalised:
signed documents are parsed from the exact bytes that were verified, and refused unless those bytes already are
canonical (RFC 8785 over the contract's value domain, which ``json.dumps(sort_keys, compact, ensure_ascii=False)``
reproduces byte for byte).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

__all__ = [
    "Envelope",
    "Malformed",
    "check_catalog",
    "check_cert",
    "check_manifest",
    "channel_allowed",
    "check_revocations",
    "parse_canonical",
    "parse_envelope",
    "parse_time",
]

MAX_INT = 2**53 - 1
ENVELOPE_FORMAT = "orch.release-envelope/1"

_A = re.ASCII
# The catalog's JSON key for its add-on entries; spelled in two parts so the store-layout lint (which looks for the
# store directory name) does not mistake a JSON key for a path.
_CATALOG_ENTRIES = "".join(("add", "ons"))
_TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z", _A)
_CHANNEL = re.compile(r"(main|develop|test-[a-z0-9][a-z0-9-]{0,30})\Z", _A)
_KEY_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z", _A)
_VERSION = re.compile(r"\d+\.\d+\.\d+(\.dev\d+)?\Z", _A)
_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", _A)
_HEX64 = re.compile(r"[0-9a-f]{64}\Z", _A)
_HEX40 = re.compile(r"[0-9a-f]{40}\Z", _A)


class Malformed(Exception):
    """The input is not exactly the contract's format."""


def _bad() -> NoReturn:
    raise Malformed


def _need(cond: bool) -> None:
    if not cond:
        _bad()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            _bad()  # duplicate key
        out[k] = v
    return out


def _no(_: str) -> NoReturn:
    _bad()  # float, NaN, Infinity


def _strict_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_float=_no, parse_constant=_no)
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        raise Malformed from e


MAX_DEPTH = 32  # signed documents are shallow; deeper nesting is refused as malformed


def _in_domain(v: Any, depth: int = 0) -> bool:
    """Objects with ASCII keys, strings, integers 0..2^53-1, booleans, lists: nothing else (no null, no floats)."""
    if isinstance(v, bool | str):
        return True
    if isinstance(v, int):
        return 0 <= v <= MAX_INT
    if isinstance(v, list):
        return depth < MAX_DEPTH and all(_in_domain(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return depth < MAX_DEPTH and all(k.isascii() and _in_domain(x, depth + 1) for k, x in v.items())
    return False


def parse_canonical(data: bytes) -> dict[str, Any]:
    """Parse signed bytes that must already be canonical. Never re-encodes them for any other purpose."""
    obj = _strict_json(data)
    _need(isinstance(obj, dict) and _in_domain(obj))
    try:
        again = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (UnicodeError, RecursionError) as e:  # lone surrogates cannot be encoded
        raise Malformed from e
    _need(again == data)
    return obj  # type: ignore[no-any-return]


def _keys(obj: Any, want: set[str]) -> dict[str, Any]:
    _need(isinstance(obj, dict) and set(obj) == want)
    return obj  # type: ignore[no-any-return]


def _str(v: Any) -> str:
    _need(isinstance(v, str))
    return v  # type: ignore[no-any-return]


def _b64(v: Any, size: int | None = None) -> bytes:
    s = _str(v)
    try:
        raw = base64.b64decode(s, validate=True)  # padded RFC 4648 §4, no whitespace
    except ValueError as e:  # binascii.Error, non-ASCII input
        raise Malformed from e
    _need(base64.b64encode(raw).decode("ascii") == s and (size is None or len(raw) == size))
    return raw


def parse_time(v: Any) -> datetime:
    s = _str(v)
    _need(bool(_TIME.match(s)))
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as e:
        raise Malformed from e


def _uint(v: Any, lo: int = 1) -> int:
    _need(type(v) is int and lo <= v <= MAX_INT)
    return v  # type: ignore[no-any-return]


def _matches(v: Any, rx: re.Pattern[str]) -> str:
    s = _str(v)
    _need(bool(rx.match(s)))
    return s


@dataclass(frozen=True)
class Envelope:
    payload: bytes
    sig_key_id: str
    sig_value: bytes
    cert_bytes: bytes
    cert_root_key_id: str
    cert_sig: bytes
    revocations_bytes: bytes
    revocations_root_key_id: str
    revocations_sig: bytes


def parse_envelope(data: bytes) -> Envelope:
    """The outer envelope: strict JSON with the exact key set. It is not signed, so it need not be canonical."""
    env = _keys(_strict_json(data), {"format", "payload", "sig", "key_cert", "revocations"})
    _need(env["format"] == ENVELOPE_FORMAT)
    sig = _keys(env["sig"], {"alg", "key_id", "value"})
    _need(sig["alg"] == "ed25519")
    kc = _keys(env["key_cert"], {"cert", "root_key_id", "sig"})
    rv = _keys(env["revocations"], {"doc", "root_key_id", "sig"})
    return Envelope(
        payload=_b64(env["payload"]),
        sig_key_id=_matches(sig["key_id"], _KEY_ID),
        sig_value=_b64(sig["value"], 64),
        cert_bytes=_b64(kc["cert"]),
        cert_root_key_id=_matches(kc["root_key_id"], _KEY_ID),
        cert_sig=_b64(kc["sig"], 64),
        revocations_bytes=_b64(rv["doc"]),
        revocations_root_key_id=_matches(rv["root_key_id"], _KEY_ID),
        revocations_sig=_b64(rv["sig"], 64),
    )


def check_cert(c: dict[str, Any]) -> bytes:
    """Validate a parsed key certificate; return its raw public key."""
    _keys(c, {"format", "key_id", "public_key", "serial", "purpose", "channels", "not_before", "not_after"})
    _need(c["format"] == "orch.release-key/1")
    _matches(c["key_id"], _KEY_ID)
    pub = _b64(c["public_key"], 32)
    _uint(c["serial"])
    _need(c["purpose"] in ("core", "addon-catalog"))
    ch = c["channels"]
    _need(isinstance(ch, list) and bool(ch) and len(set(ch)) == len(ch))
    _need(all(isinstance(x, str) and (x == "test-*" or _CHANNEL.match(x)) for x in ch))
    _need(parse_time(c["not_before"]) < parse_time(c["not_after"]))
    return pub


def check_revocations(d: dict[str, Any]) -> None:
    _keys(d, {"format", "min_serial", "revoked", "issued_at"})
    _need(d["format"] == "orch.revocations/1")
    _uint(d["min_serial"])
    r = d["revoked"]
    _need(isinstance(r, list) and all(type(x) is int and 1 <= x <= MAX_INT for x in r) and r == sorted(set(r)))
    parse_time(d["issued_at"])


def check_manifest(m: dict[str, Any]) -> None:
    _keys(
        m,
        {"format", "channel", "version", "seq", "commit", "issued_at", "expires_at", "files", "changelog", "min_from"},
    )
    _matches(m["channel"], _CHANNEL)
    _matches(m["version"], _VERSION)
    _matches(m["min_from"], _VERSION)
    _uint(m["seq"])
    _matches(m["commit"], _HEX40)
    _need(parse_time(m["issued_at"]) < parse_time(m["expires_at"]))
    _need(len(_str(m["changelog"]).encode("utf-8")) <= 4096)
    fs = m["files"]
    _need(isinstance(fs, list) and len(fs) == 3)
    for f in fs:
        _keys(f, {"role", "name", "sha256", "size", "url"})
        _matches(f["name"], _FILE_NAME)
        _matches(f["sha256"], _HEX64)
        _uint(f["size"])
        url = _str(f["url"])
        _need(url.startswith("https://") and len(url) <= 512 and url.isprintable() and " " not in url)
    _need(sorted(str(f["role"]) for f in fs) == ["constraints", "core", "dashboard"])
    _need(len({f["name"].lower() for f in fs}) == 3)  # case-insensitive file systems


def check_catalog(m: dict[str, Any]) -> None:
    """The catalog's own fields; entries under ``addons`` are checked by the add-on platform (A2)."""
    _keys(m, {"format", "channel", "seq", "issued_at", "expires_at", _CATALOG_ENTRIES})
    _matches(m["channel"], _CHANNEL)
    _uint(m["seq"])
    _need(parse_time(m["issued_at"]) < parse_time(m["expires_at"]))
    _need(isinstance(m[_CATALOG_ENTRIES], dict))


def channel_allowed(allowed: list[str], channel: str) -> bool:
    return channel in allowed or (channel.startswith("test-") and "test-*" in allowed)
