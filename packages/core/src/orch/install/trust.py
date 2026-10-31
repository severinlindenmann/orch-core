"""Explicit trust for a Publish owned by the edge OS user."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from .cli import _HUB_ID, Ctx, _admin_hub, _card_hub_id
from .parts import InstallError
from .state import load, save


def check_hub_id(value: str) -> str:
    if not isinstance(value, str) or not _HUB_ID.fullmatch(value):
        raise InstallError("--trust-hub needs exactly 32 lowercase hex characters")
    return value


def fingerprint(origin: str | None, hub_id: str) -> str | None:
    if not origin:
        return None
    from orch.hubclient.connectlink import Link, fetch_card
    from orch.hubclient.errors import HubRefused

    try:
        card = fetch_card(Link(origin, hub_id, "", None))
        if _card_hub_id(json.dumps(card)) != hub_id:
            return None
        # _card_hub_id accepts the id only when it equals wire.hub_id(hk_root_pub), so the fingerprint is bound to it.
        # Same hub-key fingerprint as the core signing review (host.signcontent._fp).
        return hashlib.sha256(card["o"]["hk_root_pub"].encode("utf-8")).hexdigest()[:12]
    except (HubRefused, OSError, ValueError, TypeError, KeyError):
        return None


def apply(ctx: Ctx, env: Mapping[str, str], *, fetch_fingerprint: bool = True) -> None:
    st = load(ctx.layout)
    wanted = st.get("edge_trust")
    if not wanted:
        return
    hub_id = check_hub_id(wanted["hub_id"])
    pub = st["parts"].get("publish")
    if not pub:
        return
    old = pub.get("pinned_hub_id")
    if old and old != hub_id:
        _admin_hub(ctx, pub, ctx.layout.data("publish"), "untrust", check_hub_id(old), env)
        pub["pinned_hub_id"] = None  # the old trust is gone even if the new one fails below
        save(ctx.layout, st)
    _admin_hub(ctx, pub, ctx.layout.data("publish"), "trust", hub_id, env)
    pub["pinned_hub_id"] = hub_id
    save(ctx.layout, st)
    if old and old != hub_id:
        ctx.say(f"Publish no longer trusts hub {old}; it now trusts {hub_id}")
    fp = fingerprint(wanted.get("origin"), hub_id) if fetch_fingerprint else None
    ctx.say(f"Publish trusts hub {hub_id}" + (f" (hub card fingerprint {fp})" if fp else ""))


def hub_hint(ctx: Ctx) -> None:
    hub = load(ctx.layout)["parts"].get("hub")
    if not hub:
        return
    rc, out = ctx.run_stdout([hub["binary"], "keys", "--state", str(ctx.layout.data("hub")), "card"], ctx.env)
    hub_id = _card_hub_id(out) if rc == 0 else None
    if hub_id and _HUB_ID.fullmatch(hub_id):  # a hint only: a bad card skips it, never fails the setup
        # Publish exposes no read endpoint listing trusted hubs; its local admin list needs the other user's data.
        ctx.say(
            "On the computer user that runs Publish, run: "
            f"`orch setup --server --role edge --trust-hub {hub_id}` (your other edge settings stay)."
        )
