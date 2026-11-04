"""One-time invitation links for people: join, password-and-code reset, and recovery (M2-5, #1399 #1401).

The token is 32 random bytes (URL-safe base64) that exists in the link's FRAGMENT and in one POST body, nowhere else: the
store keeps only its SHA-256 hash. Redemption is atomic and single use (see :class:`Invites.redeem`). Whoever the token
is for, an unknown, an expired and a used token are the same ``bad_invite``."""

from .store import (
    CHALLENGE_TTL_MS,
    INVITE_TTL_MS,
    INVITES_FILE,
    Invite,
    InviteError,
    Invites,
    proof_digest,
    token_hash,
)

__all__ = [
    "CHALLENGE_TTL_MS",
    "INVITES_FILE",
    "INVITE_TTL_MS",
    "Invite",
    "InviteError",
    "Invites",
    "proof_digest",
    "token_hash",
]
