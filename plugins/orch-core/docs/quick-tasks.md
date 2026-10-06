# Quick tasks

A quick task is a one-line job too small for a ticket: a typo, a version bump, a dead import, a stale fixture. It
has no requirements, plan, task list or verdict. Someone adds the line, an agent claims it, does it and closes it
with one line of proof. A size limit keeps it small: past the limit the task is marked "outgrew it", the agent
stops, and you decide what happens next.

Quick tasks are off until you turn them on.

## Turning them on

Quick tasks are switched by the `quick-tasks` default addon, like Terminals and Graph: enable it per workspace in
Mission Control → **Workspace & addons**, or run `orch addon enable quick-tasks` in your own terminal (agents can
never enable an addon). Once on, **Quick tasks** appears in the menu under **Addons**; while it is off the menu has no
entry, `/quick` answers 404, `orch quick` refuses changes, `orch next` offers none and the commit-msg hook does not
accept `Q-…` keys.

The addon's settings (Workspace & addons) are yours, kept in your orch config dir outside the repository:

| Setting | Default | Meaning |
|---|---|---|
| Agents may add quick tasks | off | Whether agents may run `orch quick add` themselves |
| Most commits | 1 | Commits naming the task before it outgrows the limit |
| Most files | 3 | Files those commits and the working tree change, orch's own records left out |

The rest is in `orchestrator/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `quick.next` | `"idle"` | `orch next`: `idle` lists quick tasks only when no ticket is ready, `first` before the tickets, `never` not at all |
| `quick.prefix` | `"Q"` | Keys are `Q-12`; never the ticket prefix (then `QT`) |
| `quick.max_artifacts` | `5` | Artifacts per quick task |
| `quick.claim_minutes` | `30` | A claim with no new claim this long is stale |

`quick.enabled`, `quick.agents_add`, `quick.max_commits` and `quick.max_files` from the first version are ignored.

## The commands

| Who | Command |
|---|---|
| anyone | `orch quick [--all] [--json]`, `orch quick show Q-12` |
| you; agents only with `agents_add` | `orch quick add "Fix the typo in README" [--area README.md]` |
| an agent (or you) | `orch quick claim Q-12`, `orch quick release Q-12` |
| the agent holding it (or you) | `orch quick done Q-12 -m "fixed, 4f2a91c"` (an agent's note is required) |
| the agent holding it (or you) | `orch quick artifact add Q-12 after.png --label "After"`, `--url <link>` |
| anyone | `orch quick promote Q-12`: a backlog chore with the line as its title |
| anyone | `orch quick near L-0042 [--path src/x]` |
| you only | `orch quick reopen Q-12 [-m "why"]`, `orch quick drop Q-12` |

Commits name the task like a ticket: `Q-12 fix the typo`, with the body lines the workspace asks for. The commit-msg
hook accepts the key only while the task is open and has not outgrown its limit.

## How agents pick them up

1. **You start them on one.** Each open task in Mission Control has **Open in Mission Control** (a session in
   Terminals; needs the `terminals` addon and tmux) and **Open in terminal** (your own terminal, as Start agent on a
   ticket opens it, set in `~/.config/orch/launch.json`). The agent gets the task's key and the steps, never its text,
   and claims it itself. A task someone already holds, or one that outgrew its limit, offers no start; an outgrown
   one offers **Make it a ticket** instead. The task's page also shows the prompt to paste yourself.
2. **No ticket is ready.** `orch next` falls through to quick tasks (`quick.next`), each row marked `"quick": true`.
3. **Already nearby.** Once the agent's own ticket is in testing, `orch quick near <ticket>` lists the open quick
   tasks whose area lies in the files that ticket's commits changed. The agent takes them one at a time, in their
   own commits, while it waits for your verdict.

Rules `orch quick claim` enforces for agents:

- never while a ticket the session holds is still in progress or waiting;
- one quick task at a time;
- never one the same session filed itself;
- never one that outgrew its limit.

The order is: tasks you added before tasks agents added, then oldest first.

## The size limit

`orch quick done` counts the commits whose subject names the task (any local branch) and the files they changed,
plus what is changed but not committed yet (an agent that may not commit leaves its work in the tree). Files under
`orchestrator/` do not count. Over either limit, the task is saved as "outgrew it", the claim is released, and the
command fails with what it found. The agent stops there.

You then choose, on the task's page or in your terminal:

- **Make it a ticket** (`orch quick promote`): a backlog chore; its requirements still need your approval.
- **Let it finish** (`orch quick reopen`): the limit no longer applies to this task.
- **Drop it** (`orch quick drop`).

## Where they are kept

One JSON file per task in `orchestrator/.state/quick/` (so parallel branches never edit the same file), the counter
in `.state/quick-counter.json`, artifacts in `orchestrator/artifacts/Q-12/`. All are shared records: commit them
with your work. Every change is an event (`quick.added`, `quick.claimed`, `quick.done`, `quick.outgrew`, ...) in
`.state/events.jsonl`, with `data.quick` naming the task. The SessionStart hook prints the open count and the quick
task the session holds.
