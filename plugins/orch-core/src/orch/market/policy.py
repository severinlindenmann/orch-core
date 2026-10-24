"""Who may use which marketplace, decided before any network access (E1; spec 4.2, 9, 10).

The class and the hub decision are functions of the URL and the signed log only, so a refused call connects to
nothing. The hub's policy object (``orch.hub-policy/1``) is G7 (#928): until it can be read, a workspace that is
joined to a hub allows nothing (fail closed, I6), the official marketplace included; and the V3-D32 case (a company
marketplace, hub allows it and pins its publisher key) is the same refusal, not a path."""

from __future__ import annotations

from . import MarketError, constants
from .url import canonical_market_url


def current(*, hub_joined: bool) -> None:
    """The hub's policy object as this release can read it (design G3 4.6, the G7 hook): ``None`` is standalone (no hub,
    nothing restricts). Joined to a hub, the policy is G7's (#928) and not readable here yet, so everything is
    ``market.not_allowed`` (fail closed, I6). G7 replaces this body and returns a ``Policy``."""
    if hub_joined:
        raise MarketError("market.not_allowed")
    return None


def evaluate(stage: str, policy: object, req: dict[str, object]) -> None:
    """Judge ``req`` (``{url, id, version, capabilities, index_key, publisher_key}``) at ``stage`` (``add``, ``index``,
    ``entry``, ``manifest``, ``grant`` or ``run``) against ``policy``. G3 calls it at every one of its stages;
    standalone (``policy is None``) allows. A policy that exists but cannot be judged here is refused: only G7 can
    produce one, and its body replaces this one."""
    if policy is not None:
        raise MarketError("market.not_allowed")


def gate(url: str, *, hub_joined: bool, dev_url: str | None = None) -> str:
    """The canonical ``url``'s class: ``"official"``, or ``"dev"`` for ``dev_url`` (the sandbox's test marketplace of a
    dev host, C5; the caller passes it only for a sandbox workspace). Order: canonical form, hub, class. Refusals:
    ``market.malformed``, ``market.not_allowed`` (a hub is joined and its policy is not readable here yet),
    ``market.community_unisolated`` (anything else, until per-add-on user isolation, #287)."""
    canonical_market_url(url)
    evaluate("add", current(hub_joined=hub_joined), {"url": url})
    if url == constants.OFFICIAL_URL:
        return "official"
    if dev_url is not None and url == dev_url:
        return "dev"
    raise MarketError("market.community_unisolated")
