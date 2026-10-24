"""The hub's marketplace policy, as the host reads it (V3 G7, orch-core#928; spec 9; design D14).

Two readers, one rule: a policy comes only from a verified, pinned snapshot
(:func:`orch.hubclient.snapshot.hub_market_policy`), and every failure to read one on a hub-joined host is
``market.not_allowed``, never "standalone".

* :func:`stored` (worker thread, NO network): the policy of the last pinned snapshot, or ``None`` when the signed
  log says no hub is joined. ``None`` is trusted only after the LOG agrees: a joined workspace whose ``hub.json``
  is missing, unbound or rewritten raises (the D14 delta note), because the file is local and unsigned.
* :func:`refresh_now` (off the worker): the fresh ``snapshot_get`` that spec 5 step 5 needs before any network
  access to a marketplace. It has no cached fallback: when it fails the caller refuses (``market.not_allowed``).
* :func:`run_gate` and :func:`paused` (worker, no network): the run-time view of an add-on and the pause the
  person sees.

Core imports no hub code (D79): ``orch.hubclient`` is the core's own client of the hub's wire contract."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from orch.market import MarketError, policy

from .errors import HostError

if TYPE_CHECKING:
    from orch.hubclient.marketpolicy import MarketPolicy

    from .workspace import Call, WorkspaceService

__all__ = ["UNREADABLE", "facts", "paused", "allowed", "refresh_now", "run_gate", "running", "stored", "view"]


class _Unreadable:
    """What a read-only listing keeps when the policy could not be read: every use of it is ``market.not_allowed``."""

    def __repr__(self) -> str:
        return "UNREADABLE"


UNREADABLE = _Unreadable()


def stored(service: WorkspaceService) -> MarketPolicy | None:
    """The policy of the pinned snapshot now (verified again), ``None`` for a workspace the signed log says is not
    joined, ``MarketError`` ``market.not_allowed`` for every joined state that cannot be verified. Worker thread."""
    from orch.hubclient import HubRefused, snapshot

    from . import hubops, marketops

    joined = marketops.hub_joined(service)
    try:
        state = hubops._load_state(service)  # noqa: SLF001
        f = hubops.facts_for(service)
        got = snapshot.hub_market_policy(state, now_ms=f.now_ms, dev_mode=f.dev_mode)
    except (HubRefused, HostError):
        raise MarketError("market.not_allowed") from None
    except Exception:  # noqa: BLE001 - anything unexpected on a read of the policy refuses
        raise MarketError("market.not_allowed") from None
    if got is None:
        if joined:  # the log says joined, the local record says no hub: not standalone (D14 delta)
            raise MarketError("market.not_allowed")
        return None
    return got[1]


def facts(service: WorkspaceService) -> dict[str, Any]:
    """What the off-worker half needs and only the worker can say: is a hub joined (per the log), is this a dev
    workspace."""
    from . import hubops, marketops

    return {"joined": marketops.hub_joined(service), "dev": hubops.workspace_dev(service)}


def refresh_now(call: Call, dev: bool) -> str | None:
    """A fresh ``snapshot_get`` over the hub link (spec 5 step 5), off the workspace worker. Returns ``None`` when the
    snapshot is pinned (or there is nothing to fetch: the next :func:`stored` decides), else the fixed code of the
    refusal (``HubRefused.code``). A failure leaves the old pin untouched and is NOT a cached fallback."""
    from orch.hubclient import HubRefused

    from . import hubops

    service = call.service
    try:
        state = hubops._load_state(service)  # noqa: SLF001
        if state is None or state.status != "bound":
            return None
        wsk_pub, sign = hubops._wsk_sign(service)  # noqa: SLF001
        now = lambda: int(service.clock() * 1000)  # noqa: E731
        hubops._refresh(call, {"dev": dev}, state, wsk_pub, sign, now, None)  # noqa: SLF001
    except HubRefused as exc:
        return exc.code
    except HostError:
        return "unreadable"
    except Exception:  # noqa: BLE001 - a link that fails in any way is a refusal, never a fallback
        return "unreachable"
    return None


def view(pol: MarketPolicy | None | _Unreadable) -> dict[str, Any]:
    """The hub half of ``market.installed`` (what the person sees when add-ons are paused): ``standalone`` (no hub),
    ``verified`` (the pinned snapshot verifies; a policy may still pause single add-ons) or ``unverified`` (a joined
    host with no verifiable snapshot, an expired card for one: EVERY add-on is paused, built-ins included, until the
    host reconnects, ``reconnect`` names how)."""
    if pol is None:
        return {"state": "standalone", "code": None, "reconnect": None}
    if isinstance(pol, _Unreadable):
        return {"state": "unverified", "code": "market.not_allowed", "reconnect": "orch hub refresh"}
    return {"state": "verified", "code": None, "reconnect": None}


def run_gate(service: WorkspaceService) -> Callable[[Any], None]:
    """The run-time check for ``orch.addons.runner.call(gate=...)``: before each run of an add-on, built-ins included,
    on the last pinned snapshot with no network, and with the install record as it is now. A refusal is "paused by
    hub policy"; undoing the policy change (or reconnecting) un-pauses it, the grant is untouched."""

    def gate(granted: Any) -> None:
        from .marketinstall import _files  # noqa: PLC0415

        pol = stored(service)  # a joined host that cannot verify raises market.not_allowed
        if pol is None:
            return
        from orch.market import ADDONS  # noqa: PLC0415

        rec = _files(service).installs()[ADDONS].get(granted.name)
        policy.check_run(
            pol, name=granted.name, version=granted.version, capabilities=sorted(granted.capabilities),
            origin=None if rec is None else rec["origin"], key=None if rec is None else rec["publisher_key"],
        )  # fmt: skip

    return gate


def running(service: WorkspaceService, name: str) -> bool:
    """THE one answer to "is this add-on on, for any host feature that runs it?": granted, enabled, not purged, and not
    paused by the hub policy now (:func:`run_gate`; an unverifiable hub pauses every add-on). Every host path that
    uses an add-on (start-agent and model routing, terminals, publish) asks this, never the grant alone."""
    from orch.addons.install import as_addons

    try:
        found = as_addons(service.store.state.workspace.addons).get(name)
    except (AttributeError, KeyError, TypeError):
        return False
    if found is None or not found.enabled or found.purged:
        return False
    return allowed(service, found)


def allowed(service: WorkspaceService, granted: Any) -> bool:
    """Does the hub policy let this grant (``name``, ``version``, ``capabilities``) run now? Fail closed."""
    try:
        run_gate(service)(granted)
    except MarketError:
        return False
    return True


def paused(service: WorkspaceService, pol: MarketPolicy | None | _Unreadable, rec: dict[str, Any] | None, model: Any,
           name: str) -> dict[str, Any] | None:  # fmt: skip
    """Is this add-on paused by the hub policy now (the run-time check on the pinned snapshot, no network)? ``None``
    when it runs, else ``{code, cause, reconnect}``: ``code`` is the refusal, ``cause`` is ``policy`` (the hub's
    policy says no) or ``unverified`` (no verified snapshot: an expired card, a hub that never answered, a reset
    record), ``reconnect`` is the command that repairs an unverified one. Built-ins have no record."""
    if model is None or model.purged:
        return None
    if isinstance(pol, _Unreadable):
        return {"code": "market.not_allowed", "cause": "unverified", "reconnect": "orch hub refresh"}
    try:
        policy.check_run(
            pol, name=name, version=model.version, capabilities=sorted(model.capabilities),
            origin=None if rec is None else rec["origin"], key=None if rec is None else rec["publisher_key"],
        )  # fmt: skip
    except MarketError as exc:
        return {"code": exc.code, "cause": "policy", "reconnect": None}
    return None
