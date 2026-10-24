"""The official marketplace: constants of this core release (spec 4.2, 15.2-2). Not read from disk, the environment,
a flag or any index. Changing them is a core release."""

from __future__ import annotations

OFFICIAL_URL = "https://github.com/severinlindenmann/orch-marketplace"

#: Canonical unpadded base64url P-256 public keys (65-byte uncompressed points): the current and the next official
#: marketplace key (rotation without an emergency release). EMPTY until the release step has created the
#: ``purpose: "marketplace"`` key and signs the index: with no key nothing can verify, so the official index is
#: ``market.index_unsigned`` (fail closed). The only verifier input is this tuple (see ``index.verify_official``).
OFFICIAL_KEYS: tuple[str, ...] = ()

#: Hosts an add-on repository may live on besides the marketplace's own host (spec 5.1). G3 uses it.
DEFAULT_GIT_HOSTS = ("github.com", "gitlab.com")
CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")

#: A refresh by an agent within this many seconds of the last fetch of the same market is skipped (E5).
AGENT_MIN_INTERVAL = 300
MAX_INDEX_BYTES = 1 << 20

#: Ids of add-ons the official marketplace ships (reserved for it: a non-official marketplace cannot claim them, spec
#: 5 step 6). A constant of this release; EMPTY until the first official add-on exists.
OFFICIAL_IDS: tuple[str, ...] = ()
#: Built-in ids that moved to the official marketplace (G6): only the official marketplace may list these.
MOVED_IDS: tuple[str, ...] = ()
#: A staged package is kept this long before the review is signed (design G3 section 2).
STAGE_TTL = 30 * 60
