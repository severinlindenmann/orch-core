"""Recover the causes of unrecorded invalidations from the events, including after a crash."""

from typing import Any

_CAUSES = {
    "branch.pushed": "new_commits",
    "policy.changed": "policy_changed",
    "member.removed": "member_changed",
    "role.changed": "member_changed",
    "device.revoked": "device_compromised",
    "restore": "restored",
}


def pending_causes(store: Any, uid: str) -> dict[str, str]:
    """Name the latest event that retired an approval, using the normal replay traversal."""
    causes: dict[str, str] = {}
    store.replay_invalidations(uid, lambda gate, typ: causes.update({gate: _CAUSES.get(typ, "content_changed")}))
    return causes
