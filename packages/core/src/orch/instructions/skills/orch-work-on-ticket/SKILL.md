---
name: orch-work-on-ticket
description: Use when you claimed or were given an orch ticket to implement: when to ask, what counts as evidence, how to hand over.
---
# Working on a ticket

Judgment rules. The loop itself is `orch help work`.

- When the person names a ticket, claim that one by its REF, not the next free one.
- Read the ticket (`orch show`) before you change anything. Do not re-ask what a decision already settles.
- Ask when a requirement reads two ways that lead to different work, a step cannot be undone or lies outside the ticket, or the choice is the person's. Otherwise decide and log it.
- Never ask for an approval, review or verdict: submitting does; then wait.
- Every question goes through `orch ask` with 2 to 6 plain-word options and a recommendation, never only in the terminal. Write the question for a non-technical reader: plain words, no ticket ids or keys, no option ids. On retry:true fix the call; if it still fails, ask without options and put the choices in the text. If it does not block you, do another task meanwhile.
- Evidence must fail if the work were wrong: a receipt of a check that exercises the criterion, not a note that says it is done.
- Task done with the run option records a receipt of the verify command; without a verify, close it plainly and add the evidence as an artifact. A refusal is retryable: change the call.
- Never mark a task done to get past it: block or skip it with a reason.
- Put results where the ticket says: in `$ORCH_WORKTREE` when there is one, else `$ORCH_WORKDIR`, and file each deliverable as a ticket artifact. Never write into the project root or `.orch/`.
- Stay inside the ticket: something useful but unrequested becomes a note or new ticket.
- Hand over for a reader with no memory: where it stands, what is next, what you ruled out.
- Submit only when every criterion has evidence; close any gap submit lists.
- As a subagent, take exactly one task, finish it and report; leave the manager's claim alone.
- A wait under an hour stays in the session; a longer one gets a follow-up (`orch help wait`), then end the session.
- Write its brief for a reader with no memory: what to check, then done / still running / failed. First commit and push WIP and log where you stand.
- To wake early, wait on an event or a webhook at the job's last step; the deadline still runs it, so check by hand.
- A follow-up session starts by reading the follow-up and the ticket; the brief is your own old note, so verify it.
