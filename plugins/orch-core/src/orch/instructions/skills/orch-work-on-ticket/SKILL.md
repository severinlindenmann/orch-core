---
name: orch-work-on-ticket
description: Use when you claimed or were given an orch ticket to implement: when to ask, what counts as evidence, how to hand over.
---
# Working on a ticket

Judgment rules. The loop itself is `orch help work`.

- When the person names a ticket, claim that one by its REF (`orch claim ABC-0001`), not the next free one.
- Read the ticket (`orch show`) before you change anything: requirements, acceptance criteria, decisions so far. Do not re-ask what a decision already settles.
- Ask instead of guessing when a requirement reads two ways that lead to different work, when a step cannot be undone or lies outside the ticket, or when the plan needs a choice that belongs to the person. Otherwise decide, and log the decision.
- Never ask for an approval, a review or a verdict ("please approve", "review the gate"): submitting puts it in front of the person; `orch wait`. A question that only asks for a decision already due is noise.
- Ask once, with options and a recommendation, then wait. If the question does not block you, work on another task meanwhile.
- Evidence must fail if the work were wrong: a receipt of a check that exercises the criterion, not a note that says it is done.
- The run option of task done runs a task's verify command and records its receipt; a task without one is closed plainly, with its evidence added as an artifact. That refusal is retryable: change the call, do not stop.
- Never mark a task done to get past it. Block or skip it and give the reason.
- Put results where the ticket says: in `$ORCH_WORKTREE` when there is one (commit there), otherwise in `$ORCH_WORKDIR`, and file each deliverable as a ticket artifact (add it with the artifact command, tied to its task and criterion). Never write into the project root or `.orch/`.
- Stay inside the ticket. Something useful but unrequested becomes a note or a new ticket.
- Hand over as if the next reader has no memory: where it stands, what is next, what you tried and ruled out.
- Submit only when every criterion has evidence. When submit lists what is missing, close that gap instead of arguing with it.
- As a subagent, take exactly one task, finish it and report; leave the manager's claim alone.
