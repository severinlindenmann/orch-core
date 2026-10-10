"""Wiring the CLI to a workspace :class:`~orch.store.Store` (C3): the three :class:`~orch.cli.main.Hooks` and the
session records under ``.state/sessions``.

``grant_valid`` knows only the context (the CLI calls it before it knows the ticket), so it checks what a grant is on
its own: it exists, the secret matches the recorded hash (constant time), it is not revoked or expired, and its
person is a current member who may run agents. Scope, verbs and ticket visibility are judged by the model at
append (``grant.scope``, ``grant.verb``), the same code that replays them.

``hooks_for(store)`` and ``records_for(store)`` serve a store that is already open; ``workspace_hooks(workspace)`` and
``workspace_records(workspace, default)`` are what ``orch.cli.main`` uses: they open the workspace only when a hook
needs it, and without a workspace the hooks are neutral (the handler then says ``not_found``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from orch.cli.main import Hooks
from orch.cli.session import FileRecords, MemoryRecords
from orch.ops import Context, Operation
from orch.ops.errors import OrchError

__all__ = ["grant_person", "hooks_for", "records_for", "workspace_hooks", "workspace_records"]


def records_for(store: Any) -> FileRecords:
    """Session records (stop rule, retry dedup) in the workspace's ``.state/sessions``, locked and atomic."""
    return FileRecords(store.state_dir / "sessions")


def grant_person(store: Any, grant: str | None) -> str:
    """The person a grant is for, after existence, secret, expiry, revocation and the person's membership.
    ``config.json`` (``agents.run_for``) is never read: it is not signed, so it decides nothing (a viewer may not run
    agents, whatever the file says). Raises ``grant.required`` or ``grant.expired``."""
    from orch.identity import Refused, parse_grant, secret_matches

    try:
        token = parse_grant(grant)
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
    return view.person


def _hooks(get_store: Callable[[], Any | None]) -> Hooks:
    def head_seq(op: Operation, args: dict[str, Any]) -> int | None:
        ref = args.get("ref")
        store = get_store()
        if not isinstance(ref, str) or store is None:
            return None
        n = store.head_seq(ref)  # takes the lock and checks the ticket (not a hint)
        return n or None

    def normalise(ref: str) -> str:
        store = get_store()
        return store.normalise_ref(ref) if store is not None else ref

    def grant_valid(ctx: Context) -> None:
        store = get_store()
        if store is not None:
            grant_person(store, ctx.grant)

    def grant_verbs(ctx: Context) -> Any:
        store = get_store()
        if store is None or not ctx.grant:
            return "agent"
        view = store.state.workspace.grants.get(ctx.grant.partition(".")[0])
        return "agent" if view is None else view.verbs

    return Hooks(head_seq=head_seq, normalise_ref=normalise, grant_valid=grant_valid, grant_verbs=grant_verbs)


def hooks_for(store: Any) -> Hooks:
    return _hooks(lambda: store)


def workspace_hooks(workspace: Any) -> Hooks:
    """Hooks over ``orch.ops.runtime.Workspace``: the store is opened on first use, once per call, and a missing
    workspace or a store that will not open leaves the hook neutral (the handler reports the cause)."""

    def get() -> Any | None:
        if workspace.root is None:
            return None
        return _guard(lambda: workspace.store)

    return _hooks(get)


def _guard(open_store: Callable[[], Any]) -> Any | None:
    from orch.cli.store_errors import to_orch_error
    from orch.store import StoreError

    try:
        return open_store()
    except StoreError as e:
        raise to_orch_error(e) from e


def workspace_records(workspace: Any, default: MemoryRecords) -> MemoryRecords:
    """``FileRecords`` in the workspace's ``.state/sessions``, or ``default`` (process memory) outside a workspace."""
    from orch.ops.runtime import records_dir

    root = workspace.root
    return FileRecords(records_dir(root)) if root is not None else default
