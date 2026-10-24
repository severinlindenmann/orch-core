"""The official marketplace: constants of this core release (spec 4.2, 15.2-2). Not read from disk, the environment,
a flag or any index. Changing them is a core release."""

from __future__ import annotations

OFFICIAL_URL = "https://github.com/severinlindenmann/orch-marketplace"

#: Canonical unpadded base64url P-256 public keys (65-byte uncompressed points): the current and the next official
#: marketplace key (rotation without an emergency release), CURRENT FIRST. EMPTY until the operator has run
#: orch-marketplace ``scripts/gen-index-keys`` (M#3, docs/signing.md): with no key nothing can verify, so the official
#: index is ``market.index_unsigned`` (fail closed). The only verifier input is this tuple (``index.official_keys``).
#: TODO(M#3, operator): paste the two public halves printed by gen-index-keys here, replacing ``()`` by
#:     ("<current public key>", "<next public key>")
#: Public keys only; never a private key. tests/market/test_pinned_keys.py checks the shape once they are set.
OFFICIAL_KEYS: tuple[str, ...] = ()

#: Hosts an add-on repository may live on besides the marketplace's own host (spec 5.1). G3 uses it.
DEFAULT_GIT_HOSTS = ("github.com", "gitlab.com")
CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")

#: A refresh by an agent within this many seconds of the last fetch of the same market is skipped (E5).
AGENT_MIN_INTERVAL = 300
MAX_INDEX_BYTES = 1 << 20

#: Ids reserved for the official marketplace (a non-official marketplace cannot claim them, spec 5 step 6). A constant
#: of this release (G6, addon-api 1.1): ``github`` and ``landing`` are reserved ahead of their packages;
#: ``model-routing`` ships first.
OFFICIAL_IDS: tuple[str, ...] = ("github", "landing", "model-routing")
#: Built-in ids that moved to the official marketplace (G6): only the official marketplace may list these.
MOVED_IDS: tuple[str, ...] = ()
#: A staged package is kept this long before the review is signed (design G3 section 2).
STAGE_TTL = 30 * 60
