"""Who may use which marketplace, decided before any network access (E1; spec 4.2, 9, 10).

The class and the hub decision are functions of the URL and the signed log only, so a refused call connects to
nothing. The hub's policy object (``orch.hub-policy/1``) is G7 (#928): until it can be read, a workspace that is
joined to a hub allows nothing (fail closed, I6), the official marketplace included; and the V3-D32 case (a company
marketplace, hub allows it and pins its publisher key) is the same refusal, not a path."""

from __future__ import annotations

from . import MarketError, constants
from .url import canonical_market_url


def gate(url: str, *, hub_joined: bool) -> str:
    """The canonical ``url``'s class (``"official"``). Order: canonical form, hub, class. Refusals:
    ``market.malformed``, ``market.not_allowed`` (a hub is joined and its policy is not readable here yet),
    ``market.community_unisolated`` (anything but the official URL, until per-add-on user isolation, #287)."""
    canonical_market_url(url)
    if hub_joined:
        raise MarketError("market.not_allowed")
    if url != constants.OFFICIAL_URL:
        raise MarketError("market.community_unisolated")
    return "official"
