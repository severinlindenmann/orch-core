"""Session grants (ticket-format §10.1 A3, D60): secret generation, ``secret_hash`` and the D60 term checks as pure
functions of values. The host calls :func:`check_grant_issued` on ``grant.issued`` and :func:`check_grant_revoker`
on ``grant.revoked``; the model supplies the role at the event's position.

* The token is ``gr_<ULID>.<b64u secret>`` (32 random bytes). Only ``secret_hash`` (``canon.grant_secret_hash``, label
  ``orch/v2/grant-secret|``) is stored; :func:`secret_matches` compares in constant time.
* Nothing here writes a secret anywhere. :func:`new_grant` returns it once for the caller to print.
* Term checks follow D60 exactly: owner and maintainer 1-24 h, ``all`` or ``workable``; member ``workable`` only, 1 h up
  to ``grant_hours``; viewer none; ``expires_at == issued_at + 3600 * hours``; ``|at - issued_at| <= 300 s``.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Any

from orch import canon, crypto

from .errors import Refused

__all__ = [
    "GRANT_SKEW_S",
    "GrantToken",
    "check_grant_issued",
    "check_grant_revoker",
    "format_grant",
    "new_grant",
    "new_ulid",
    "parse_grant",
    "parse_timestamp",
    "secret_matches",
]

GRANT_SKEW_S = 300
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TOKEN = re.compile(r"gr_([0-7][0-9A-HJKMNP-TV-Z]{25})\.([A-Za-z0-9_-]{43})")
_TS = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z")


def new_ulid(now_ms: int | None = None) -> str:
    """A 26-character Crockford ULID (48-bit millisecond time, 80 random bits)."""
    t = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= t < 2**48:
        raise ValueError("time out of range")
    n = (t << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> (5 * (25 - i))) & 31] for i in range(26))


class GrantToken(tuple):
    """``(grant_id, secret)`` with ``grant_id`` = ``gr_<ULID>`` and ``secret`` the 32 decoded bytes."""

    __slots__ = ()

    def __new__(cls, grant_id: str, secret: bytes):
        return super().__new__(cls, (grant_id, secret))

    @property
    def grant_id(self) -> str:
        return self[0]

    @property
    def secret(self) -> bytes:
        return self[1]

    def __repr__(self) -> str:  # never print the secret
        return f"GrantToken({self[0]!r}, <redacted>)"


def new_grant(now_ms: int | None = None) -> tuple[GrantToken, str, str]:
    """``(token, ORCH_GRANT value, secret_hash)`` for a fresh grant. Print the value once; store only the hash."""
    token = GrantToken("gr_" + new_ulid(now_ms), secrets.token_bytes(32))
    return token, format_grant(token), canon.grant_secret_hash(token.secret)


def format_grant(token: GrantToken) -> str:
    return f"{token.grant_id}.{crypto.b64u(token.secret)}"


def parse_grant(value: object) -> GrantToken:
    """``gr_<ULID>.<b64u of 32 bytes>``; anything else is refused (``grant.token``)."""
    m = _TOKEN.fullmatch(value) if type(value) is str else None
    if not m:
        raise Refused("grant.token")
    try:
        return GrantToken("gr_" + m.group(1), crypto.unb64u(m.group(2), 32))
    except crypto.EncodingError:
        raise Refused("grant.token") from None


def secret_matches(secret: bytes, secret_hash: str) -> bool:
    """Constant-time check of a presented secret against the stored ``secret_hash``."""
    try:
        return hmac.compare_digest(canon.grant_secret_hash(secret).encode("ascii"), secret_hash.encode("ascii"))
    except (canon.HashError, AttributeError, UnicodeEncodeError):
        return False


def parse_timestamp(value: object) -> int:
    """``YYYY-MM-DDTHH:MM:SSZ`` (UTC, seconds 00-59) to epoch seconds; ``grant.timestamp`` otherwise."""
    m = _TS.fullmatch(value) if type(value) is str else None
    if not m:
        raise Refused("grant.timestamp")
    try:
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        return int(datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp())
    except ValueError:
        raise Refused("grant.timestamp") from None


_SCOPES = ("all", "workable")


def check_grant_issued(
    event: Mapping[str, Any],
    *,
    role: str,
    at: str,
    grant_hours: int = 8,
    human_only: Collection[str] = frozenset(),
) -> None:
    """D60 terms for a ``grant.issued`` payload (``scope``, ``verbs``, ``issued_at``, ``hours``, ``expires_at``,
    ``secret_hash``) signed by a person with ``role`` at the event's position, appended at ``at``. Raises
    :class:`Refused`:

    ``grant.role`` (viewer, or an unknown role); ``grant.scope`` (not ``all``/``workable``, or ``all`` for a member);
    ``grant.hours`` (not an int, outside 1-24, or above ``grant_hours`` for a member); ``grant.issued_at`` (more than
    300 s from ``at``); ``grant.expires_at`` (not ``issued_at + 3600 * hours``); ``grant.verbs``
    (neither ``"agent"`` nor
    a list of operation names, or a human-only operation in the list); ``grant.secret_hash`` (not a hash).
    """
    if role not in ("owner", "maintainer", "member"):
        raise Refused("grant.role", f"{role!r} may not issue grants")
    if not (type(grant_hours) is int and 1 <= grant_hours <= 24):
        raise Refused("grant.hours", "grant_hours setting")
    scope, hours = event.get("scope"), event.get("hours")
    if scope not in _SCOPES or (role == "member" and scope != "workable"):
        raise Refused("grant.scope", f"{role} may issue {'workable' if role == 'member' else 'all or workable'} only")
    if type(hours) is not int or hours < 1 or hours > (grant_hours if role == "member" else 24):
        raise Refused("grant.hours", f"1 to {grant_hours if role == 'member' else 24}")
    issued = parse_timestamp(event.get("issued_at"))
    if abs(parse_timestamp(at) - issued) > GRANT_SKEW_S:
        raise Refused("grant.issued_at", "more than 300 s from the event time")
    if parse_timestamp(event.get("expires_at")) != issued + 3600 * hours:
        raise Refused("grant.expires_at", "must be issued_at + 3600 * hours")
    verbs = event.get("verbs")
    if verbs != "agent":
        if (
            type(verbs) is not list
            or not verbs
            or any(type(v) is not str for v in verbs)
            or len(set(verbs)) != len(verbs)
        ):
            raise Refused("grant.verbs")
        if any(v in human_only for v in verbs):
            raise Refused("grant.verbs", "a human-only operation is never in a grant")
    try:
        canon.parse_hash(event.get("secret_hash"))
    except canon.HashError:
        raise Refused("grant.secret_hash") from None


def check_grant_revoker(*, revoker_role: str, revoker: str, issuer: str) -> None:
    """D60: an owner revokes any grant; a maintainer or member revokes their own; a viewer none
    (``grant.revoke_role`` / ``grant.revoke_not_own``). ``revoker`` and ``issuer`` are person ids."""
    if revoker_role == "owner":
        return
    if revoker_role not in ("maintainer", "member"):
        raise Refused("grant.revoke_role", f"{revoker_role!r} revokes no grants")
    if revoker != issuer:
        raise Refused("grant.revoke_not_own")
