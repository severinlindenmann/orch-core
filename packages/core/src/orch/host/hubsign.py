"""Hub signing helpers (A1, design ~/orch-dev/state/a1-hub-optional-design.md, D83; replaces V3 C11).

A local signature is valid in EVERY hub mode, with the hub reachable or not: the workspace roster decides who may sign
locally, and the hub is an additional signer (``auth: "hub"``). There is no ``sign.hub_only`` any more. What is left:
:data:`SEALS_TO_HUB` (the modes in which a new ``WK_e`` epoch must be sealed to the hub, for ``keys.rotate``), the
key-exposure error (#1097) and the custody read. D79: this module imports no hub code.
"""

from __future__ import annotations

from typing import Any

from .errors import HostError

__all__ = ["SEALS_TO_HUB", "key_exposed_error", "local_key_exposed"]

#: modes in which the hub holds ``WK_e`` (``unknown`` fails closed, as full)
SEALS_TO_HUB = frozenset({"full", "unknown"})


def key_exposed_error() -> HostError:
    """``sign.key_exposed`` (#1097): the device key has signed evidence of convenience custody, so it cannot enter full
    hub mode or sign break-glass; a new key made with the recovery code clears it."""
    return HostError(
        "sign.key_exposed",
        "this device key was in one-click custody, so a program running as you may hold a copy: make a new key first",
        hint="orch keys custody passphrase --rotate --workspace W (needs your recovery code; works with the hub down)",
        fix=["orch", "keys", "custody", "passphrase", "--rotate"],
    )


def local_key_exposed(service: Any, person: str | None) -> bool:
    """#1097: does the signed log mark a live device of ``person`` as exposed? Read from the roster, not from custody
    files; fails closed (an unreadable roster or an unknown person counts as exposed). For ``keys.rotate``."""
    try:
        if person is None:
            return True
        devs = service.store.state.workspace.devices.values()
        return any(d.person == person and not d.removed and not d.revoked and d.exposed for d in devs)
    except Exception:  # noqa: BLE001 - fail closed
        return True


def _click_custody(service: Any) -> bool:
    from . import convenience

    return bool(convenience._click_custody(service))  # noqa: SLF001 - the one custody read: flag on, convenience, unreadable
