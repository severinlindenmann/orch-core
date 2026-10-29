"""The remote policy as the host reads it (R2 #1590, design note sections 1, 2, 5): ``effective`` (a pure read of the
model), :func:`turn_off` (the ONE off path), and the expiry sweeper (every 30 s, under the store lock)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import HostError

__all__ = ["Policy", "SWEEP_S", "Sweeper", "effective", "ensure_sweeper", "status", "sweep", "turn_off"]

LOG = logging.getLogger(__name__)
SWEEP_S = 30.0


@dataclass(frozen=True)
class Policy:
    on: bool  # on now: the policy says on and its end date is not reached
    level: str | None
    until_ms: int | None
    premises: str | None
    off_reason: str | None  # why it is off (owner | device | expired | hub_full), None if never on


def effective(remote: Mapping[str, Any] | None, now_ms: int) -> Policy:
    """The policy at ``now_ms`` from the model's ``ws.remote`` mapping (``{}`` or ``None``: never on)."""
    r = remote or {}
    until = r.get("until_ms")
    line_on = r.get("on") is True
    on = line_on and (until is None or now_ms < until)
    reason = None if on else ("expired" if line_on else r.get("off_reason"))  # past its end date, not yet swept
    return Policy(on, r.get("level"), until, r.get("premises"), reason)


def turn_off(service: Any, by: str, device: str | None = None) -> bool:
    """Record ``remote.disabled`` (H) under the store lock; the commit hook closes every stream inside that hold.

    ``by`` is ``owner`` (the owner's verb) or ``device`` (R3: an open session of a device whose person is an owner
    asks). Returns ``False`` when remote access is already off: nothing to record. A device that is not listed, or
    whose person is not an owner, is refused (``role.denied``)."""
    from . import append_host, remote_state

    with service.store.locked():
        r = remote_state(service)
        if by == "device":
            dev = r.get("devices", {}).get(device or "")
            member = service.store.state.workspace.members.get(dev["person"]) if dev else None
            if dev is None or member is None or member.role != "owner":
                raise HostError("role.denied", "only a device of an owner can turn remote access off")
        elif by not in ("owner", "expired"):
            raise ValueError(by)
        if r.get("on") is not True:
            return False
        append_host(service, "remote.disabled", by=by, **({"device": device} if by == "device" else {}))
        return True


def sweep(service: Any, now_ms: int | None = None) -> dict[str, Any]:
    """One expiry pass, on the worker thread: a policy past its end date is turned off (``by: expired``), a device
    past its ``expires_ms`` is removed (``reason: expired``). Both go through the commit hook (streams close)."""
    from . import append_host, remote_state

    now = int(service.clock() * 1000) if now_ms is None else now_ms
    out: dict[str, Any] = {"disabled": False, "removed": []}
    with service.store.locked():
        r = remote_state(service)
        until = r.get("until_ms")
        if r.get("on") is True and until is not None and now >= until:
            append_host(service, "remote.disabled", by="expired")
            out["disabled"] = True
        for did, d in sorted(r.get("devices", {}).items()):
            if d["expires_ms"] <= now:
                append_host(service, "remote.device_removed", device_id=did, reason="expired")
                out["removed"].append(did)
    if out["disabled"] or out["removed"]:
        service.sync()
    return out


class Sweeper(threading.Thread):
    """Runs :func:`sweep` on the service's worker thread every ``interval`` seconds until the service closes."""

    def __init__(self, service: Any, interval: float = SWEEP_S) -> None:
        super().__init__(name=f"remote-sweeper-{service.workspace_id[:8]}", daemon=True)
        self.service, self.interval = service, interval
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            if getattr(self.service, "closing", False) or self.service.store is None:
                return
            try:
                self.service.submit(sweep, self.service).result(timeout=60)
            except Exception:  # noqa: BLE001 - a failed pass is retried at the next tick
                LOG.exception("remote sweep failed")

    def stop(self) -> None:
        self.stop_event.set()


def ensure_sweeper(service: Any) -> None:
    """Start the sweeper once per service, when there is a policy or a device to sweep."""
    from . import remote_state

    if getattr(service, "_remote_sweeper", None) is not None or getattr(service, "closing", False):
        return
    r = remote_state(service)
    if r.get("on") is True or r.get("devices"):
        sweeper = service._remote_sweeper = Sweeper(service, SWEEP_S)  # noqa: SLF001
        sweeper.start()


def status(service: Any, now_ms: int) -> dict[str, Any]:
    """``remote.status``: the policy, the devices (with the six fingerprint words) and what is outstanding."""
    from orch.hubclient import wire

    from . import enrollment, remote_state, wrk

    r = remote_state(service)
    p = effective(r, now_ms)
    devices = [
        {
            "device_id": did, "name": d["name"], "person": d["person"], "presence_kind": d["presence_kind"],
            "level_cap": d["level_cap"], "added_ms": d["added_ms"], "expires_ms": d["expires_ms"],
            "words": wire.fingerprint_words(bytes.fromhex(d["fingerprint"])),
        }
        for did, d in sorted(r.get("devices", {}).items())
    ]  # fmt: skip
    scalar = wrk.load(service)
    return {
        "on": p.on, "level": p.level, "until_ms": p.until_ms, "premises": p.premises, "off_reason": p.off_reason,
        "agents_risk": r.get("agents_risk"), "hub_mode": r.get("hub_mode"),
        "devices": devices, "device_version": r.get("device_version", 0),
        "codes": len(enrollment.outstanding(service, now_ms)),
        "wrk_fp": None if scalar is None else wrk.wrk_fp(wrk.public(scalar)).hex(),
    }  # fmt: skip
