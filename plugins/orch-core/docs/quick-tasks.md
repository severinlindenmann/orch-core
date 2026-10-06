# Quick tasks

A quick task is a one-line job too small for a ticket: a typo, a version bump, a dead import, a stale fixture. It
has no requirements, plan, task list or verdict. Someone adds the line, an agent claims it, does it and closes it
with one line of proof. A size limit keeps it small: past the limit the task is marked "outgrew it", the agent
stops, and you decide what happens next.

Quick tasks are off until you turn them on.

## Turning them on

```bash
orch quick enable                     # in your own terminal; signed into the approval ledger
orch quick enable --agents-add        # agents may also add quick tasks themselves
orch quick enable --max-files 5       # change the size limit (default: 1 commit, 3 files)
orch quick disable                    # anyone may turn them off
```

Or use **Quick tasks** in Mission Control's menu: the page has a Turn on button and, once they are on, a Settings
card. A paired phone or remote device cannot change these settings.

The switch, whether agents may add tasks and the two limits are signed into the approval ledger, as `widgets.html`
is. A config edit alone never turns quick tasks on, never lets agents add them and never raises a limit above what
you signed: `orch quick` then says "config.json asks for them, but no signed decision backs it". The guard also
refuses an agent's edit of the `quick` block in `orchestrator/config.json`.

`orchestrator/config.json`:

| Key | Default | Meaning |
|---|---|---|
| `quick.enabled` | `false` | On or off (signed) |
| `quick.agents_add` | `false` | Agents may add quick tasks (signed) |
| `quick.max_commits` | `1` | Most commits naming the task (signed: the lower of config and signature counts) |
| `quick.max_files` | `3` | Most files those commits and the working tree change, orch's own records left out (signed, as above) |
| `quick.next` | `"idle"` | `orch next`: `idle` lists quick tasks only when no ticket is ready, `first` before the tickets, `never` not at all |
| `quick.prefix` | `"Q"` | Keys are `Q-12`; never the ticket prefix (then `QT`) |
| `quick.max_artifacts` | `5` | Artifacts per quick task |
| `quick.claim_minutes` | `30` | A claim with no new claim this long is stale |

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
| you only | `orch quick reopen Q-12 [-m "why"]`, `orch quick drop Q-12`, `orch quick enable` |

Commits name the task like a ticket: `Q-12 fix the typo`, with the body lines the workspace asks for. The commit-msg
hook accepts the key only while the task is open and has not outgrown its limit.

## How agents pick them up

1. **You start them on one.** The task's page in Mission Control has a prompt to paste into a new agent session.
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
