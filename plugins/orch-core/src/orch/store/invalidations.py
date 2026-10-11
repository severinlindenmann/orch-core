"""Recover the causes of unrecorded invalidations from the events, including after a crash."""

from typing import Any

from orch.model import WORKSPACE, advance, replay

_CAUSES = {
    "branch.pushed": "new_commits",
    "policy.changed": "policy_changed",
    "member.removed": "member_changed",
    "role.changed": "member_changed",
    "device.revoked": "device_compromised",
    "restore": "conflict_resolved",
}


def pending_causes(store: Any, uid: str) -> dict[str, str]:
    """Replay only when records are owed. For accumulated voids, name the latest event that retired an approval."""
    current = store._state
    events = [
        (log, e)
        for log in (WORKSPACE, *sorted(store._loaded))
        for e in store._read_events(log, 1, store._logs[log].seq)
    ]
    events.sort(
        key=lambda pair: (
            (pair[1]["seq"], 0, "", "", pair[1]["seq"])
            if pair[0] == WORKSPACE
            else (pair[1]["ws_seq"], 1, pair[1]["at"], pair[0], pair[1]["seq"])
        )
    )
    state = replay(
        [],
        {},
        verifier=store._verifier,
        now=current.now,
        expected_workspace_id=store.workspace_id,
        expected_genesis=store._pin,
        verb_events=current._ctx.verb_events,
    )
    before: dict[str, set[str]] = {}
    causes: dict[str, str] = {}
    for log, event in events:
        state = advance(state, event, log=log)
        ticket = state._core.tickets.get(uid)
        if ticket is None:
            continue
        for gate, value in ticket.gates.items():
            if value.pending_void - before.get(gate, set()):
                causes[gate] = _CAUSES.get(event["type"], "content_changed")
            before[gate] = set(value.pending_void)
    return causes
