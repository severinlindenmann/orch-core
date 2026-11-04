---
name: orch-work-on-ticket
description: Use when you claimed or were given an orch ticket to implement: when to ask, what counts as evidence, how to hand over.
---
# Working on a ticket

Judgment rules; the loop is `orch help work`.

- When the person names a ticket, claim it by its REF, not the next free one.
- Read the ticket (`orch show`) before changing anything; do not re-ask what a decision settles.
- Ask when a requirement reads two ways, a step is irreversible or outside the ticket, or the choice is the person's. Otherwise decide and log it.
- Never ask for an approval, review or verdict: submitting does; then wait.
- Ask every question through `orch ask`, never only in the terminal: 2 to 6 plain-word options and a recommendation. Write it for a non-technical reader: no ticket ids, keys or option ids. On retry:true fix the call, else ask without options, choices in the text. If it does not block you, do another task meanwhile.
- Evidence must fail if the work were wrong: a receipt of a check of the criterion, not a note saying it is done.
- Task done with the run option records a receipt of the verify; without one, close it plainly and add the evidence as an artifact. A refusal is retryable: change the call.
- Never mark a task done to get past it: block or skip it, with a reason.
- Put results where the ticket says (`$ORCH_WORKTREE`, else `$ORCH_WORKDIR`); file each deliverable as a ticket artifact. Never write into the project root or `.orch/`.
- Write for the person in plain words: "the work folder", never workdir, env names, paths or ids.
- Stay inside the ticket: unrequested work becomes a note or new ticket.
- Hand over for a reader with no memory: where it stands, what is next, what you ruled out.
- Submit only when every criterion has evidence; close any gap submit lists.
- As a subagent, take exactly one task, finish it and report; leave the manager's claim alone.
- A wait under an hour stays in the session; a longer one gets a follow-up (`orch help wait`), then end it.
- Write its brief for a reader with no memory: what to check, then done/running/failed. First commit and push WIP and log where you stand.
- To wake early, wait on an event, a webhook or an add-on event; the deadline still runs it. Verify the result.
- A follow-up session first reads the follow-up and the ticket; the brief is your own old note, so verify it.
