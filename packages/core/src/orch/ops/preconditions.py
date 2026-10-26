"""The named preconditions an operation declares in ``pre``. Each is checked against derived state when the
operation runs (C6/C7); here they are names with one line of meaning, so ``describe`` and the tests have a
vocabulary."""

from __future__ import annotations

PRECONDITIONS: dict[str, str] = {
    "workspace_exists": "an orch workspace is found from the working directory",
    "ticket_exists": "the referenced ticket exists",
    "ticket_visible": "the ticket's visibility lets the actor see it",
    "ticket_open_for_work": "the ticket is not done or closed",
    "ticket_in_testing": "the ticket is in testing",
    "session_holds_claim": "the session holds the ticket's claim",
    "claim_free_or_takeover": "no live claim, or the call is a takeover with a reason",
    "task_exists": "the referenced task exists on the ticket",
    "task_lease_free": "no other session leases the task",
    "session_holds_lease": "the session leases the task",
    "acceptance_exists": "the referenced acceptance criterion exists",
    "all_ac_have_evidence": "every acceptance criterion has evidence",
    "question_open": "the question is asked and not yet answered",
    "gate_current": "the gate hash, generation and policy hash match the current ones",
    "approver_eligible": "the person is an eligible approver of the gate",
    "role_allows": "the person's workspace role allows the action",
    "ticket_owner_or_maintainer": "the person owns the ticket or is an owner or maintainer",
    "grant_valid": "the grant is valid, unexpired, covers the ticket and the verb",
    "user_presence": "the person confirmed with user presence",
    "unattended_scope": "without a grant: visibility workspace only, new question ids only, no --ac or --task",
    "unattended_quota": "the unattended quotas are not used up",
    "base_rev_tracked": "orch has the session's base_rev for the touched section or field",
    "text_clean": "the text passes the text rules",
    "no_workspace": "no workspace exists yet at the target",
    "v1_workspace": "the path holds a v1 workspace",
    "addon_exists": "the addon is installed or was granted",
}
