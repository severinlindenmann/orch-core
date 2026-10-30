"""The public webhook inbox, contract (protocol.md section 10.11, orch-core#1285).

A sender (GitHub, a CI step, a Databricks job) rings ``POST <hub public url>/h/<token>``; the hub answers 204 and
keeps ONE row per hook: ``(token_hash, source, kind, received_at)``. The body is read only to verify the signature and
is dropped. The workspace pulls the rows over its authenticated link (``hook_pull`` / ``hook_ack``) and takes the
follow-up id and the source from ITS OWN records, found by ``token_hash``; nothing the hub says in text is trusted.

Everything here is pure (no I/O): the op body checks, the token and secret shapes, and the three signature schemes.
The vectors are ``contracts/hub/vectors_hooks.json`` (made by ``make_vectors_hooks.py``); orch-core replays them with
its own hand-written port.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any

SOURCES = ("github", "generic", "databricks")  # closed; the ring's `source` is one of these
KINDS = ("hook.rang",)  # closed; the only thing a ring says
KIND = "hook.rang"
TOKEN_PREFIX = "hh_"
TOKEN_RE = re.compile(r"hh_[A-Za-z0-9_-]{43}")  # "hh_" + b64u(32 random bytes), no padding
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # b64u(32 random bytes); the TEXT is the HMAC key / Basic password
HASH_RE = re.compile(r"[0-9a-f]{64}")
FOLLOWUP_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
BASIC_USER = "orch"  # Databricks: the Basic-auth username is fixed, the password is the secret
GENERIC_WINDOW_S = 300  # `X-Orch-Signature: t=<unix>,v1=<hex>`: t within +/- 5 minutes of the hub clock
BODY_CAP = 256 * 1024  # bytes; Content-Length above this is 413 and nothing is read
MAX_HOOKS_PER_WORKSPACE = 64
MAX_EXPIRY_MS = 31 * 24 * 3600 * 1000  # a hook lives at most 31 days from hook_put
PULL_MAX = 50
HOOK_CODES = ("hook_exists", "too_many_hooks", "unavailable", "bad_expiry")  # refusals of hook_put (protocol 14)


def token_hash(token: str) -> str:
    """sha256(token text) as lowercase hex: the only form of the token the hub (and the ledger) ever keeps."""
    if TOKEN_RE.fullmatch(token or "") is None:
        raise ValueError("not a hook token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _hash(x: Any) -> bool:
    return type(x) is str and HASH_RE.fullmatch(x) is not None


HASH_RE_CHECK = _hash  # the per-field check the link op table uses


def put_ok(b: Any) -> bool:
    """``hook_put`` body: ``{token_hash, followup, source, secret, expires_ms}`` + optional ``replaces`` (a hash).
    ``followup`` and ``source`` are display/bookkeeping text of the WORKSPACE; the ring never carries them."""
    if type(b) is not dict or not {"token_hash", "followup", "source", "secret", "expires_ms"} <= set(b):
        return False
    if set(b) - {"token_hash", "followup", "source", "secret", "expires_ms", "replaces"}:
        return False
    return (
        _hash(b["token_hash"])
        and type(b["followup"]) is str
        and FOLLOWUP_RE.fullmatch(b["followup"]) is not None
        and type(b["source"]) is str
        and b["source"] in SOURCES
        and type(b["secret"]) is str
        and SECRET_RE.fullmatch(b["secret"]) is not None
        and type(b["expires_ms"]) is int
        and b["expires_ms"] >= 1
        and ("replaces" not in b or (_hash(b["replaces"]) and b["replaces"] != b["token_hash"]))
    )


def put_reply_ok(b: Any) -> bool:
    return (
        type(b) is dict
        and set(b) == {"url_base"}
        and type(b["url_base"]) is str
        and re.fullmatch(r"https?://[^\s/?#]+/h/", b["url_base"]) is not None
    )


def ring_ok(r: Any) -> bool:
    """One ring row of a ``hook_pull`` reply. No follow-up id, no free text."""
    return (
        type(r) is dict
        and set(r) == {"seq", "token_hash", "source", "kind", "received_at"}
        and type(r["seq"]) is int
        and r["seq"] >= 1
        and _hash(r["token_hash"])
        and r["source"] in SOURCES
        and r["kind"] in KINDS
        and type(r["received_at"]) is int
        and r["received_at"] >= 0
    )


def pull_reply_ok(b: Any) -> bool:
    return (
        type(b) is dict
        and set(b) == {"rings", "head"}
        and type(b["rings"]) is list
        and len(b["rings"]) <= PULL_MAX
        and all(ring_ok(r) for r in b["rings"])
        and type(b["head"]) is int
        and b["head"] >= 0
        and [r["seq"] for r in b["rings"]] == sorted({r["seq"] for r in b["rings"]})
    )


def ack_reply_ok(b: Any, seq: int | None = None) -> bool:
    return type(b) is dict and set(b) == {"acked"} and type(b["acked"]) is int and (seq is None or b["acked"] == seq)


# --- the three signature schemes (the secret's TEXT is the key; see SECRET_RE) -------------------------------------


def _key(secret: str) -> bytes:
    if SECRET_RE.fullmatch(secret or "") is None:
        raise ValueError("not a hook secret")
    return secret.encode("ascii")


def github_mac(secret: str) -> hmac.HMAC:
    """HMAC-SHA256 over the raw body; compare with ``X-Hub-Signature-256: sha256=<hex>``. Feed the body in chunks."""
    return hmac.new(_key(secret), digestmod=hashlib.sha256)


def generic_mac(secret: str, t: int) -> hmac.HMAC:
    """HMAC-SHA256 over ``<t>.`` then the raw body; compare with ``v1`` of ``X-Orch-Signature: t=<t>,v1=<hex>``."""
    m = hmac.new(_key(secret), digestmod=hashlib.sha256)
    m.update(str(t).encode("ascii") + b".")
    return m


_GITHUB = re.compile(r"sha256=([0-9a-f]{64})")
_GENERIC = re.compile(r"t=([0-9]{1,12}),v1=([0-9a-f]{64})")


def parse_github(header: str | None) -> str | None:
    m = _GITHUB.fullmatch(header or "")
    return m.group(1) if m else None


def parse_generic(header: str | None) -> tuple[int, str] | None:
    m = _GENERIC.fullmatch(header or "")
    return (int(m.group(1)), m.group(2)) if m else None


def generic_fresh(t: int, now_s: int) -> bool:
    return abs(now_s - t) <= GENERIC_WINDOW_S


def basic_ok(header: str | None, secret: str) -> bool:
    """Databricks: ``Authorization: Basic base64("orch:<secret>")``. BOTH fields are compared, in constant time and
    without a short circuit; any malformed header is False."""
    h = header or ""
    user = pw = b""
    good = h[:6] == "Basic " and len(h) <= 512
    if good:
        try:
            raw = base64.b64decode(h[6:].strip(), validate=True)
        except ValueError:
            raw, good = b"", False
        user, sep, pw = raw.partition(b":")
        good = good and bool(sep)
    a = hmac.compare_digest(user, BASIC_USER.encode())
    c = hmac.compare_digest(pw, secret.encode("ascii", "replace"))
    return good and a and c
