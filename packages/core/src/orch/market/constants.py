"""The official marketplace: constants of this core release (spec 4.2, 15.2-2). Not read from disk, the environment,
a flag or any index. Changing them is a core release."""

from __future__ import annotations

OFFICIAL_URL = "https://github.com/severinlindenmann/orch-marketplace"

#: Canonical unpadded base64url P-256 public keys (65-byte uncompressed points): the current and the next official
#: marketplace key (rotation without an emergency release), CURRENT FIRST. Made by orch-marketplace
#: ``scripts/gen-index-keys`` (M#3, owner decision V3-D39); the private halves are never in a repository. The only
#: verifier input is this tuple (``index.official_keys``); any other signer gives ``market.index_unsigned``.
OFFICIAL_KEYS: tuple[str, ...] = (
    "BFh8x9dLhMubfTXleS0Bt6Ix-yu9ZiE18B0xRUUNWqsgZdOwt5DZX_Gqs5Ri3p2coFdLk9GhA-Q5WV8Ee0e05l4",  # current
    "BM-BTpDMGAU68ywX392c5JuCjjwOkkdccxmNiOTvcX2GzIgEM2FjFH56hihyYn7pepkd8HYMolWSzM01L_B0Udw",  # next
)

#: Hosts an add-on repository may live on besides the marketplace's own host (spec 5.1). G3 uses it.
DEFAULT_GIT_HOSTS = ("github.com", "gitlab.com")
CASE_INSENSITIVE_HOSTS = ("github.com", "gitlab.com", "bitbucket.org")

#: A refresh by an agent within this many seconds of the last fetch of the same market is skipped (E5).
AGENT_MIN_INTERVAL = 300
MAX_INDEX_BYTES = 1 << 20

#: Ids reserved for the official marketplace (a non-official marketplace cannot claim them, spec 5 step 6). A constant
#: of this release (G6, addon-api 1.1): ``github`` and ``merge-queue`` are reserved ahead of their packages;
#: ``model-routing`` ships first.
OFFICIAL_IDS: tuple[str, ...] = ("github", "merge-queue", "model-routing")
#: Built-in ids that moved to the official marketplace (G6): only the official marketplace may list these.
MOVED_IDS: tuple[str, ...] = ()
#: A staged package is kept this long before the review is signed (design G3 section 2).
STAGE_TTL = 30 * 60

#: Where an add-on's dependencies may be downloaded from, per lockfile kind (spec 8.2, design G4). A host is compared
#: whole (never a suffix or prefix); the node registry here is the one ``orch.addons.lockfile`` lets a lock name.
DEP_HOSTS: dict[str, tuple[str, ...]] = {
    "python-requirements": ("pypi.org", "files.pythonhosted.org"),
    "node-package-lock": ("registry.npmjs.org",),
}
