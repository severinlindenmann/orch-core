"""Who may use which marketplace and add-on under the hub's policy (G7, orch-core#928; spec 4.2, 9; design D14).

The policy is ``orch.hub-policy/1`` as :func:`orch.hubclient.snapshot.hub_market_policy` returns it from a verified,
pinned snapshot (a :class:`~orch.hubclient.marketpolicy.MarketPolicy`; this module only reads its fields). ``None`` is
standalone: the owner decides alone (D79) and nothing here restricts. Everything below can only REFUSE; the human grant
stays. The class and the policy decision are functions of the URL and the policy only, so a refused call connects to
nothing.

:func:`evaluate` is the one rule table. A caller names the ``stage`` it is at and passes only what that stage knows:

* ``add``: the marketplace ``url`` is listed (``market.not_allowed``). Step 5, before any network access.
* ``index``: listed; a ``company`` marketplace's index was signed by a pinned key (``market.index_unsigned``).
* ``entry``: listed; ``id`` or ``id@version`` is not in ``addons.block`` (``market.blocked``); ``addons.allow`` is
  ``null`` or has the ``id`` (``market.addon_not_allowed``). Step 6, after the entry is selected.
* ``publisher``: listed; a ``company`` marketplace's entry is signed by a pinned key (``market.publisher_unpinned``).
  The pin replaces TOFU for that URL. Step 7.
* ``manifest``: the capabilities are within ``ceiling`` (``market.capability_ceiling``). Step 8.
* ``install``: all of the above, in this order (the shared ``evaluate`` vectors of D14).
* ``grant``: the re-check on the staged bytes and the grant event (steps 14 and 18): listed, block, allow, publisher
  (``company``), ceiling.
* ``run``: before each run of an add-on, on the pinned snapshot: the block list and the ceiling for every add-on
  (built-ins have no ``url``), and for an add-on from a marketplace also that its origin is still listed, ``allow``,
  and a ``company`` publisher key (the key the install recorded) that is still pinned. Refused = "paused by hub
  policy"; undoing the policy change un-pauses it, the grant is untouched."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import MarketError, constants
from .url import canonical_market_url

if TYPE_CHECKING:
    from orch.hubclient.marketpolicy import MarketPolicy

__all__ = ["PAUSED", "STAGES", "check_run", "evaluate", "from_raw", "gate"]

STAGES = ("add", "index", "entry", "publisher", "manifest", "install", "grant", "run")
#: the words an add-on shows while a policy refuses it (a closed set: the code is the catalog's)
PAUSED = "paused by hub policy"


def _listed(policy: MarketPolicy, url: Any) -> Any:
    entry = policy.market(url) if isinstance(url, str) else None
    if entry is None:
        raise MarketError("market.not_allowed")
    return entry


def _allowed(policy: MarketPolicy, req: dict[str, Any]) -> None:
    ident, version = req.get("id"), req.get("version")
    if ident in policy.block or f"{ident}@{version}" in policy.block:
        raise MarketError("market.blocked")
    if policy.allow is not None and ident not in policy.allow:
        raise MarketError("market.addon_not_allowed")


def _pinned(entry: Any, key: Any) -> None:
    if entry.cls == "company" and key not in entry.keys:
        raise MarketError("market.publisher_unpinned")


def _within(policy: MarketPolicy, req: dict[str, Any]) -> None:
    if policy.ceiling is not None and not set(req.get("capabilities") or ()) <= set(policy.ceiling):
        raise MarketError("market.capability_ceiling")


def evaluate(stage: str, policy: MarketPolicy | None, req: dict[str, Any]) -> None:
    """Judge ``req`` (``{url, id, version, capabilities, index_key, publisher_key}``, the keys a stage reads) at
    ``stage`` against ``policy``. The first failing check is the answer (``MarketError``). ``policy`` ``None`` is
    standalone and allows; a joined host whose policy is unreadable never reaches here with ``None`` (it raises
    ``market.not_allowed`` where the policy is read, :func:`from_raw`)."""
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if policy is None:
        return
    if stage == "run":
        _run(policy, req)
        return
    entry = _listed(policy, req.get("url"))
    if stage in ("index", "install") and entry.cls == "company" and req.get("index_key") not in entry.keys:
        raise MarketError("market.index_unsigned")
    if stage in ("entry", "install", "grant"):
        _allowed(policy, req)
    if stage in ("publisher", "install", "grant"):
        _pinned(entry, req.get("publisher_key"))
    if stage in ("manifest", "install", "grant"):
        _within(policy, req)


def _run(policy: MarketPolicy, req: dict[str, Any]) -> None:
    url = req.get("url")
    if url:  # an add-on from a marketplace; a built-in has no origin
        entry = _listed(policy, url)
        _allowed(policy, req)
        if req.get("publisher_key") is not None:
            _pinned(entry, req["publisher_key"])
    elif req.get("id") in policy.block or f"{req.get('id')}@{req.get('version')}" in policy.block:
        raise MarketError("market.blocked")
    _within(policy, req)


def check_run(
    policy: MarketPolicy | None, *, name: str, version: str, capabilities: Any, origin: str | None, key: str | None
) -> None:
    """The run-time check of one add-on (``origin`` and ``key`` are what its install record holds, ``None`` for a
    built-in). A ``company`` origin whose record holds no pinned key is refused: it cannot have been installed under a
    pin, so the pin cannot hold now."""
    if policy is not None and origin:
        entry = policy.market(origin)
        if entry is not None and entry.cls == "company" and key is None:
            raise MarketError("market.publisher_unpinned")
    evaluate("run", policy, {"url": origin or "", "id": name, "version": version, "capabilities": capabilities,
                             "publisher_key": key})  # fmt: skip


def from_raw(raw: Any) -> MarketPolicy:
    """A policy object as the shared vectors give it (``null`` = unreadable): parsed by the one validator, and every
    failure is ``market.not_allowed``, never a policy that reads as empty or as standalone."""
    from orch.hubclient import HubRefused
    from orch.hubclient.marketpolicy import parse_policy

    if raw is None:
        raise MarketError("market.not_allowed")
    try:
        return parse_policy(raw)
    except HubRefused:
        raise MarketError("market.not_allowed") from None


def gate(url: str, *, policy: MarketPolicy | None, dev_url: str | None = None) -> str:
    """The canonical ``url``'s class: ``"official"``, ``"company"`` (the hub lists it with pinned keys, V3-D32: no OS
    isolation needed), or ``"dev"`` for ``dev_url`` (the sandbox's test marketplace of a dev host, C5; the caller
    passes it only for a sandbox workspace). Order: canonical form, hub, class. Refusals: ``market.malformed``,
    ``market.not_allowed`` (a hub policy exists and does not list the URL, official included),
    ``market.community_unisolated`` (anything else, until per-add-on user isolation, #287)."""
    canonical_market_url(url)
    evaluate("add", policy, {"url": url})
    if url == constants.OFFICIAL_URL:
        return "official"
    if policy is not None and policy.market(url).cls == "company":
        return "company"
    if dev_url is not None and url == dev_url:
        return "dev"
    raise MarketError("market.community_unisolated")
