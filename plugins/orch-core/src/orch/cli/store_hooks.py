"""Wiring the CLI to a workspace :class:`~orch.store.Store` (C3): the three :class:`~orch.cli.main.Hooks` and the
session records under ``.state/sessions``.

``grant_valid`` knows only the context (the CLI calls it before it knows the ticket), so it checks what a grant is on
its own: it exists, the secret matches the recorded hash (constant time), it is not revoked or expired, and its
person is a current member who may run agents. Scope, verbs and ticket visibility are judged by the model at
append (``grant.scope``, ``grant.verb``), the same code that replays them.
"""

from __future__ import annotations

from typing import Any

from orch.cli.main import Hooks
from orch.cli.session import FileRecords
from orch.identity import Refused, parse_grant, secret_matches
from orch.ops import Context, Operation
from orch.ops.errors import OrchError
from orch.store import Store

__all__ = ["hooks_for", "records_for"]


def records_for(store: Store) -> FileRecords:
    """Session records (stop rule, retry dedup) in the workspace's ``.state/sessions``, locked and atomic."""
    return FileRecords(store.state_dir / "sessions")


def hooks_for(store: Store) -> Hooks:
    def head_seq(op: Operation, args: dict[str, Any]) -> int | None:
        ref = args.get("ref")
        if not isinstance(ref, str):
            return None
        n = store.head_seq(ref)  # takes the lock and checks the ticket (not a hint)
        return n or None

    def grant_valid(ctx: Context) -> None:
        """Existence, secret, expiry, revocation and the person's membership. ``config.json`` (``agents.run_for``) is
        never read: it is not signed, so it decides nothing (a viewer may not run agents, whatever the file says)."""
        try:
            token = parse_grant(ctx.grant)
        except Refused:
            raise OrchError("grant.required", "ORCH_GRANT is malformed") from None
        want = store.grant_secret_hash(token.grant_id)  # under the lock, re-read from the logs
        view = store.state.workspace.grants.get(token.grant_id)
        if want is None or view is None or not secret_matches(token.secret, want):
            raise OrchError("grant.required", "ORCH_GRANT is not a grant of this workspace")
        if not view.active:
            raise OrchError("grant.expired", "the grant is expired or revoked")
        member = store.state.workspace.members.get(view.person)
        if member is None or member.role == "viewer":
            raise OrchError("grant.expired", "the grant's person may no longer run agents")

    return Hooks(head_seq=head_seq, normalise_ref=store.normalise_ref, grant_valid=grant_valid)
