# Schedules (concept, not built)

A proposed default addon, `schedules`, that starts agent work without anyone typing: on a clock, on an event,
or as a recurring ticket. Visual concept with clickable Mission Control mockups:
https://claude.ai/artifact/5zDZLJ1DDbym2FRchdknfU

## Three kinds

| Kind | Starts on | Runs | Yields |
|---|---|---|---|
| Schedule | every N min/h, daily or weekdays at, cron; active hours | one skill from `.claude/skills/` | nothing (quiet), a finding on Today, or a backlog ticket |
| Listener | an orch event, an addon snapshot item (failed run, new issue), a file change; filter and cooldown | one skill, with the event as input | a finding, or a log or artifact on the linked ticket |
| Recurring ticket | weekly, monthly, or N days after the last one was done | no session: files a ticket from a template | a ticket that runs the normal loop |

Example: "Check inbox" runs the workspace skill `triage-inbox` every hour, 08:00 to 19:00 on weekdays, reads new
mail and proposes tickets for the mails that need work. You pick which to file on Today.

## How a run works

1. **Trigger**: the clock reaches the next run, an event matches a listener, or a human presses Run now.
2. **Check (core)**: the schedule is armed by a signed charter in the human's ledger; the definition file and every
   file of the skill still match the hashes signed; inside active hours; budget left; no run of it still going.
   A failed check skips the run and logs why.
3. **Run**: one headless session (`claude -p`) through the factory runner's launch path: private tmux socket,
   absolute program paths, `env -i`, `--setting-sources user`, the permission hook as the gate, killed at the time
   cap. Actor `schedule:<id>`.
4. **Hand over**: quiet, a finding (a `PendingDecision` on Today), a backlog ticket (only if the charter allows), or
   a note/artifact on a linked ticket.

The scheduler loop lives in core next to the factory runner, because addons cannot start processes. The addon is
the switch and the pages, the same split as `terminals`.

## Definition

One file per schedule in `orchestrator/schedules/<id>.yaml`, committed with the tickets:

```yaml
name: Check inbox
kind: schedule
skill: triage-inbox
when: {every: 1h, between: "08:00-19:00", days: [mon, tue, wed, thu, fri]}
may: [report, file-ticket]
limits: {runs_per_day: 12, minutes_per_run: 5, tickets_per_day: 5}
ticket_defaults: {labels: [support], epic: L-0040}
memory: last-run
```

Being armed is not in the file. It is a signed ledger entry bound to the file's hash and the skill's hash, so an
agent editing either one pauses the schedule ("skill changed, review to arm again") instead of changing what runs.

## Rules

- **Humans arm, agents propose.** `arm`, `pause`, `resume`, `run-now`, budget raises and standing approvals are
  human-only and guard-denied. Agents may `orch schedule propose <file>`, which puts a card on Today.
- **A run is not a human.** It can file backlog tickets, log, attach and ask; never approve, answer or give a verdict.
- **Budgets that hold.** Runs per day, minutes per run and tickets per day are counted in markers beside the ledger.
- **Only while Mission Control runs.** Missed runs are not caught up one by one: on start, each schedule runs at most
  once and reports how many it skipped. An OS service unit can come later.

## CLI

```
orch schedule list [--json]            anyone
orch schedule runs <id> [--last 20]    anyone
orch schedule propose <file>           anyone
orch schedule arm <id>                 human only
orch schedule pause|resume <id>        human only
orch schedule run-now <id>             human only
```

## Phases

1. Schedules: definition, charter, hashes, interval/cron, read-and-report only, Schedules page and Today card.
2. Tickets from runs: file backlog tickets with a daily cap, multi-item findings, run memory.
3. Listeners: orch events and addon snapshots as sources, filters, cooldown, notes on linked tickets.
4. Recurring tickets: templates, standing approval per template, "after the last one is done" cadence.

## Open questions

- A `schedule` addon capability so custom addons can offer listener sources, or only core events and provider
  snapshots?
- MCP connectors (a mail run needs one) while the runner launches with `--strict-mcp-config`: allow named servers per
  schedule in the charter?
- A token budget per run next to minutes, fed by `ticket-usage`?
- Findings on a paired phone, and may the phone file the proposed tickets?
