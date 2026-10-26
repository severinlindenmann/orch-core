---
name: orch-refine-ticket
description: Use when a ticket is rough and needs requirements, acceptance criteria and a task plan before anyone implements it.
---
# Refining a ticket

Judgment rules. The steps are in `orch help refine`.

- Requirements state outcomes a reviewer can check, not activities. Write what must be true afterwards.
- An acceptance criterion is checked by one command or one artifact. "Works well" is not a criterion; "the refresh finishes in under 5 minutes on the test set" is.
- A task is small enough to finish in one sitting, ordered, and proves at least one criterion with a verify command. A last task "test everything" proves nothing.
- Every criterion is proven by some task; every task proves a criterion or is needed by one that does.
- Write out of scope what you are tempted to add. It is the cheapest way to keep the ticket small.
- Ask before building a plan on a guess, but only the one to three questions whose answers change the plan. Ask through `orch ask`, never only in the terminal (the person does not read it): 2 to 6 options in plain words and a recommendation.
- A plan gate needs the Plan, its Decisions (what you chose and why, even if short) and at least one task; a requirements gate needs its sections and one criterion. The next line of status names the first gap. Without Decisions the person sees "Add Decisions first" and has nothing to approve.
- A verify is one command run without a shell: no &&, ||, |, ;, > or $( ). Wrap a script as sh -c '...'. Adding a task refuses a bare operator.
- Fill every section and criterion the gate needs, then stop and `orch wait`: the person approves or requests changes on the gate in Approvals. Never ask a question ("please review", "approve?") for that, it only duplicates the real approval. An incomplete gate cannot be approved.
- Once approved, do not reword the approved sections quietly: a change voids the approval and sends the ticket back. Say why you change it.
