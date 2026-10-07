"""One policy for a decision that arrives through the remote bridge: the workspace's per-kind phone switch (the same
one the signed phone path reads) AND the paired device's scope must both allow it. The signed phone-decision path
(orch.remote.verify) is separate and unchanged: its own time windows and its refusal of `move` stay.

A bridged post is a request the dashboard performed for a paired device; it is not a signed phone decision. Its
event names the device (via "device:<label>", data.device) and `orch check` does not count it as verified-human
evidence beyond what an ordinary dashboard action already is.
"""
from __future__ import annotations

# The kinds a phone switch covers (Workspace -> Phones). A bridged `move` has no switch: its scope alone decides.
SWITCHED_KINDS = ("answer", "request_changes", "approve", "verdict", "ticket_request")


def allows(root, kind: str | None, origin, needed) -> bool:
    """`origin` (a reach.RemoteOrigin) has at least scope `needed`, and the workspace's phone switch for `kind` is
    on. A kind that is not one of SWITCHED_KINDS is decided by the scope alone (None); an unknown kind, an
    unreadable settings file or any error answers no."""
    try:
        if origin.scope < needed:
            return False
        if kind is None:
            return True
        if kind not in SWITCHED_KINDS:
            return False
        from orch.remote import store
        return store.permissions(root).get(kind) is True
    except Exception:  # noqa: BLE001 - fail closed
        return False
