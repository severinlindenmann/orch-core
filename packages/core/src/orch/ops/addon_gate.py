"""The one availability rule of add-on operations (AP D5 step 1): an operation declared with ``addon=X`` runs only
while add-on X is on, that is granted, enabled and not purged. ``orch.ops.runtime.guarded`` applies it to every such
operation (CLI, host, batches); the few operations that are only partly an add-on's (``new --label quick``,
``set labels=quick``) call :func:`require_on` themselves."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orch.ops.errors import OrchError

__all__ = ["is_on", "off_error", "off_message", "require_on"]


def is_on(addons: Any, name: str) -> bool:
    """Is the add-on ``name`` granted, enabled and not purged? No grant means off (there is no legacy default)."""
    a = (addons or {}).get(name)
    if a is None:
        return False
    get = a.get if isinstance(a, Mapping) else (lambda k, d=None: getattr(a, k, d))
    return bool(get("enabled", False)) and not get("purged", False)


def off_message(name: str) -> str:
    return f"the {name} add-on is off in this workspace"


def off_error(name: str) -> OrchError:
    """``addon.off`` naming the add-on, and what a person does about it (an agent only tells the person)."""
    return OrchError(
        "addon.off",
        off_message(name),
        hint=f"tell the person; do not work around it. An owner turns {name} on in Add-ons (orch addon grant {name})",
    )


def require_on(addons: Any, name: str) -> None:
    if not is_on(addons, name):
        raise off_error(name)
