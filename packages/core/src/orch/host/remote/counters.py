"""The FIDO2 signature counters of the remote devices (R3, #1591; hub contract 10.2c "Presence assertions").

One file beside the device list (``<state>/hosts/<ws>/remote/counters.json``, 0600): the highest counter the host has
accepted per device. The session open and every presence batch compare-and-set it through :func:`cas` under ONE lock,
so a counter value is accepted once, whichever of them sees it first. A counter that is not greater is ``replay``
(unless both the held and the new one are 0: an authenticator without a counter)."""

from __future__ import annotations

import threading
from typing import Any

from orch.hubclient.errors import HubRefused

__all__ = ["cas", "held", "record"]

_MU = threading.RLock()


def _doc(service: Any) -> dict[str, Any]:
    from ..custody import read_json
    from . import wrk

    doc = read_json(wrk.state_dir(service) / "counters.json", service.host_uid, limit=1 << 20)
    if not isinstance(doc, dict) or doc.get("v") != 1 or not isinstance(doc.get("fido2"), dict):
        return {"v": 1, "fido2": {}}
    return doc


def held(service: Any, device_id: str) -> int:
    with _MU:
        n = _doc(service)["fido2"].get(device_id, 0)
    return n if type(n) is int and n >= 0 else 0


def record(service: Any, device_id: str, count: int) -> None:
    """Store ``count`` for ``device_id`` (the enrollment's first value, or a CAS that passed)."""
    from ..custody import ensure_private_dir, write_private
    from . import wrk

    with _MU:
        doc = _doc(service)
        doc["fido2"][device_id] = count
        ensure_private_dir(wrk.state_dir(service), service.host_uid)
        write_private(wrk.state_dir(service) / "counters.json", doc)


def cas(service: Any, device_id: str, new: int) -> int:
    """Accept ``new`` as the device's counter: it must be greater than the held one (or both 0). Returns it.
    Raises ``HubRefused("replay")`` otherwise; nothing is stored then."""
    with _MU:
        cur = held(service, device_id)
        if (cur or new) and new <= cur:
            raise HubRefused("replay")
        if new != cur:
            record(service, device_id, new)
        return new


def lock() -> Any:
    """The one lock of the counter store (a caller that verifies an assertion against :func:`held` and then calls
    :func:`cas` holds it across both, so two verifications of one counter value cannot both pass)."""
    return _MU
