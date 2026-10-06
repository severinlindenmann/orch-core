# Schedules

Agent work that starts without anyone typing: a workspace skill on a clock ("check the support inbox every hour"),
a skill when an orch event happens ("a ticket moved to testing"), or a recurring ticket ("dependency update every
Monday"). You arm each one once. Every run stays inside orch's rules: it reads and reports, and whatever it found that
needs you lands on Today, where you decide. The first visual concept is at
https://claude.ai/artifact/5zDZLJ1DDbym2FRchdknfU.

Schedules is the default addon `schedules`, **off until you enable it** per workspace in Workspace & addons. It needs
tmux, and runs only while Mission Control (`orch serve`) runs.

## The three kinds

| Kind | Starts on | Runs | Ends with |
|---|---|---|---|
| `schedule` | `when`: `every` (5 min to 12 h) within `between` hours, `at` given times, `weekly` on a day, or a `cron` line; optional `days` | one skill from `.claude/skills/<name>/`, as a headless agent session | a report: quiet, or findings for you |
| `listener` | `on`: an orch event (`ticket.moved` with `to`, `gate.approved` with `gate`, `ticket.created`, `question.asked`, `verdict.given`, `artifact.added`), then a `cooldown` | one skill, told the event's kind, ticket key and status | a report: quiet, or findings for you |
| `recurring` | `when`, like a schedule | no session | a finding on Today: the template's ticket, which you file with one click |

## A schedule is a file

One file per schedule, `orchestrator/schedules/<id>.yaml`, committed with the tickets. An agent or you may write it;
`orch schedule check` says whether orch can read it. It runs only once you arm it.

```yaml
# orchestrator/schedules/check-inbox.yaml
name: Check inbox
kind: schedule
skill: triage-inbox              # .claude/skills/triage-inbox/SKILL.md
when: {every: 1h, between: "08:00-19:00", days: [mon, tue, wed, thu, fri]}
limits: {runs_per_day: 12, minutes_per_run: 5}
mcp: [gmail]                     # optional: servers from your schedule-mcp.json
model: claude-sonnet-5-5         # optional
```

```yaml
# orchestrator/schedules/smoke-on-testing.yaml
name: Smoke test on testing
kind: listener
skill: smoke-test
on: {event: ticket.moved, to: testing}
cooldown: 30m
```

```yaml
# orchestrator/schedules/deps-weekly.yaml
name: Weekly dependency update
kind: recurring
when: {weekly: mon, at: "07:00"}
ticket:
  title: "Update dependencies, week {week}"   # also {date}, {month}, {year}
  ask: Bump minor versions and run the tests.
  type: chore
  priority: normal
```

Limits: `runs_per_day` 1 to 96 (default 24), `minutes_per_run` 1 to 60 (default 10). Times are this machine's local
time. Unknown keys, a bad cadence, a missing skill, a link anywhere in the skill folder, a skill without `SKILL.md`,
more than 64 files or more than 1 MiB make a schedule "needs a fix"; it never runs.

## Arming: yours, signed, pinned

`orch schedule arm <id>` in your own terminal (it shows both hashes and asks for the typed id), or **Arm** on the
Schedules page. Arming signs a ledger entry for this checkout holding the sha256 of the definition file and of every
file of its skill. If either changes afterwards, the schedule shows **changed** and nothing runs (a running session is
stopped) until you look and arm it again, the same way a changed addon must be trusted again. An agent therefore
cannot change what runs by editing the skill or the file.

- **Pause** (`orch schedule pause <id>`, or Pause on the page) takes power away, so anyone may, agents included. It is
  signed like any switch off. **Resume** is arming again, and yours.
- **Run now** (`orch schedule run-now <id>`, or the page) asks the runner for one run in its next round, counted
  against today's budget. Yours.
- A schedule armed now starts from now: slots before it never run.

## How a run goes

Every 20 seconds the dashboard's runner looks at each armed schedule:

1. **Due?** A slot of `when` passed since the last round, a listener's event matched after its cursor and its cooldown
   passed, or you asked for a run. Slots missed while Mission Control was closed are not caught up one by one: the
   newest runs once and says how many it stands for. Events that match while a run goes on fold into the next run.
2. **May it run?** The charter matches the files, the ledger is whole, the addon is on, today's `runs_per_day` is not
   used up (counted in markers beside the ledger, so editing state cannot reset it), no run of the same schedule is
   still going, at most 2 runs go at once, and your user-scope Claude settings carry orch's guard. A run that may not
   start is recorded as **skipped** with the reason.
3. **Run.** One headless session in a tmux server of its own (a socket in the guarded permits folder of the orch config
   dir), the same launch path as the AI Factory runner: `claude` and `env` resolved to trusted absolute paths, `env -i`
   with a fixed PATH and a short list of variables, `--setting-sources user` (project settings and `.mcp.json`, which
   agents can write, are ignored), `--strict-mcp-config`, and one tool allowance, `orch schedule report`. The prompt is
   built in: it names the skill, the run and, for a listener, the event's kind, ticket key and status; never ticket
   text. A run past its `minutes_per_run` is stopped and marked failed.
4. **Report.** The run reports once with the token in its prompt: `orch schedule report <token> --quiet --summary "..."`,
   or findings as YAML on stdin (`--file -`). A session that ends without a report is a failed run.

```yaml
summary: 11 mails since 12:00, 2 need work.
findings:                        # at most 10
  - title: CSV export drops umlauts
    text: Northwind sent a sample file; reproducible.
    ticket:                      # optional
      title: Fix umlauts in CSV export
      ask: What should be done and why.
      type: bug                  # feature, bug, chore, spike, investigation
      priority: high             # low, normal, high, urgent
```

Results: **quiet**, **finding**, **failed** or **skipped**. Run records stay beside the ledger, outside the
repository (the newest 200, plus any with an open finding).

## Findings: a run proposes, you decide

Open findings show on Today under **From schedules** and on the Schedules page. **File ticket in backlog** creates the
proposed ticket (or one from the finding's title and text) as you, in backlog, with the run named in its Ask; the
run's text goes through the same neutralising as an imported issue, so it can never forge a section. **Dismiss**
closes the finding. Filing is bound to the finding as the card showed it. A run itself can never file, approve,
answer, give a verdict or move a ticket. From the terminal: `orch schedule findings`, `orch schedule file <run> <finding>`,
`orch schedule dismiss <run> <finding>`.

## MCP servers for a run

A run gets no MCP server unless its definition names one in `mcp`, and then only from your own
`schedule-mcp.json` in the orch config dir (`$ORCH_STATE_DIR`, else `$XDG_CONFIG_HOME/orch`, else `~/.config/orch`),
in Claude Code's format:

```json
{"mcpServers": {"gmail": {"command": "npx", "args": ["-y", "some-gmail-mcp"]}}}
```

The runner writes a per-run config with only the named servers and passes it with `--mcp-config`. A name that is not
in your file fails the run with that reason. The guard keeps agents away from the orch config dir. Which tools of the
server a run may call is up to your user-scope permission settings: a headless run is denied anything they do not
allow.

## Mission Control

- **Schedules** in the menu (while the addon is on): counts, open findings, every schedule with its trigger, last
  result, next run and state; the chosen one with its skill, budget, the last 24 runs as a strip, what arming signs and
  Arm, Run now or Pause; and the run log.
- **Today**: the From schedules cards, and the addon's tile with how many schedules are armed, open findings and the
  next run.
- From a paired remote device: the page is a read (Look), Pause and Dismiss need Decide, filing a ticket needs Operate,
  Run now needs Type, and arming needs Type with a fresh assertion (as starting an AI Factory does).

## Commands

```
orch schedule list [--json]                  anyone
orch schedule show <id> [--json]             anyone
orch schedule check [<id>]                   anyone: is the definition valid, is the skill there
orch schedule runs [<id>] [--last 20]        anyone
orch schedule findings                       anyone
orch schedule pause <id>                     anyone
orch schedule report <token> ...             the run itself
orch schedule arm|resume <id>                human only
orch schedule run-now <id>                   human only
orch schedule file|dismiss <run> <finding>   human only
```

The guard denies the human-only ones to agents, as it does `orch approve`.

## Limits, stated plainly

- Runs happen only while `orch serve` runs on this machine. Nothing runs on a laptop that sleeps.
- One checkout runs its schedules: the charter is bound to this checkout, so another clone of the workspace needs its
  own arming.
- A run is an agent with your user-scope Claude permissions. The guard, the pinned skill and the report-only shape keep
  it to reading and proposing; they are best-effort checks, not an OS sandbox, the same as for every agent orch starts.

## Not built yet

- Listeners on addon items (a failed Databricks run, a new GitHub issue). Today listeners hear orch's own events.
- A run filing backlog tickets itself under a daily cap, and a standing approval for a recurring ticket's
  requirements. Today you file every ticket.
- Token budgets per run, findings on a paired phone, and an OS service so runs go on without Mission Control.
