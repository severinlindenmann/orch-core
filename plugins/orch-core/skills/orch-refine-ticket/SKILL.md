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
- Ask before building a plan on a guess, but only the one to three questions whose answers change the plan. Offer options and a recommendation.
- A plan gate needs the Plan section, its Decisions section (what you chose and why, even if short: `orch section set decisions --file -`) and at least one task. A requirements gate needs its sections and at least one acceptance criterion. `orch status` and a refused submit name the first thing still missing (`next:`); the dashboard shows the person "Add Decisions first" and gives them no action until it is there.
- A task's verify is ONE command, run without a shell: no `&&`, `||`, `|`, `;`, `>`, `<`, `&` or `$(...)`. If you need any of them, wrap the whole script: `--verify "sh -c 'test -f index.html && grep -q title index.html'"`. `task add` refuses a bare shell operator and says so.
- Fill every section and criterion the gate needs, then stop and `orch wait`: the person approves or requests changes on the gate in Approvals. Never ask a question ("please review", "approve?") for that, it only duplicates the real approval. An incomplete gate cannot be approved.
- Once approved, do not reword the approved sections quietly: a change voids the approval and sends the ticket back. Say why you change it.
