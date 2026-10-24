"""The add-on marketplace, core side (V3 G2; docs/architecture/orch-v2-marketplace.md).

``url`` (the one canonical URL function), ``constants`` (the official URL and key list: constants of this release,
read from nowhere else), ``index`` (strict parse, schema, rules, signature), ``address`` (where the installer may
connect), ``fetch`` (the closed fetch: ``orch.gitsafe`` does the git), ``state`` (the host's records). Nothing here
imports hub code (D79); the hub gate is a function of the signed log, made by the host (``orch.host.marketops``).
"""

from __future__ import annotations

#: the index's list key (a named constant: the store-layout lint matches the bare word as a path component)
ADDONS = "addons"


class MarketError(Exception):
    """One refusal. ``code`` is a ``market.*`` code of the catalog; the text is the catalog's fixed one, never index
    or package content. ``reason`` is a fixed word that tells apart the causes of one code (``market.env_failed``)."""

    def __init__(self, code: str, reason: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
