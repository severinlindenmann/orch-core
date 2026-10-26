"""Schedules gate, time parsing and view-based lookup shared by follow-up operations."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from orch.ops import views
from orch.ops.errors import OrchError
from orch.store import StoreError

ADDON = "schedules"
MESSAGE = "Schedules is switched off in this workspace; an owner can turn it on in Add-ons"


def is_on(addons):
    addon = (addons or {}).get(ADDON)
    if addon is None:
        return False
    get = addon.get if isinstance(addon, Mapping) else lambda key, default=None: getattr(addon, key, default)
    return bool(get("enabled", False)) and not get("purged", False)


def require_on(addons):
    if not is_on(addons):
        raise OrchError("followup.off", MESSAGE)


def due(args, now):
    if ("in" in args) == ("at" in args):
        raise OrchError("invalid.input", "give exactly one of --in or --at")
    try:
        if "in" in args:
            m = re.fullmatch(r"([0-9]+)([mhd])", args["in"])
            if not m:
                raise ValueError
            dt = datetime.fromtimestamp(now, UTC) + timedelta(
                seconds=int(m[1]) * {"m": 60, "h": 3600, "d": 86400}[m[2]]
            )
        else:
            raw = args["at"]
            m = re.fullmatch(r"\+([0-9]+)d ([0-9]{2}):([0-9]{2})", raw)
            if m:
                local = datetime.fromtimestamp(now)
                dt = (local + timedelta(days=int(m[1]))).replace(
                    hour=int(m[2]), minute=int(m[3]), second=0, microsecond=0
                )
            elif re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?", raw):
                dt = datetime.fromisoformat(raw)
            else:
                raise ValueError
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        raise OrchError("invalid.input", "use --in 90m|36h|2d or --at YYYY-MM-DDTHH:MM[:SS] or '+2d 09:00'") from None


def local_time(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")


def tickets(c):
    return (v for _, v in views.verified(c, (r[0] for r in c.store.index.query("SELECT uid FROM tickets"))))


def find(c, fid):
    for v in tickets(c):
        for f in v.followups:
            if f["id"] == fid:
                return v, f
    raise OrchError("not_found", f"no follow-up {fid}")


def add(p, event):
    try:
        return p.add(event)
    except StoreError as e:
        if e.code == "followup.denied":
            denial = e.detail.partition(":")[0]
            code = denial if denial in ("too_soon", "too_far", "bad_brief") else "denied"
            raise OrchError(f"followup.{code}", e.detail) from e
        raise
