# Multi-orchestrator: agents, helpers, other agent tools and jobs (31 Oct 2026)

Status: **design draft for the owner**, 31 Oct 2026. It came out of the owner's question whether orch should run on
[herdr](https://github.com/herdrdev/herdr) (no, §2) and what orch needs to spawn helpers on any agent tool and account,
and to show all of it live (focus mode) and afterwards (ticket). Visual drafts were challenged by Codex
(`~/orch-dev/state/codex/focus-live-2221.md`); its changes are folded in. Part of #1290. Related: #2 (AI Factory),
#1854 (blocked vs finished), #1306 (focus mode hardening), [orch-v2-harnesses.md](../architecture/orch-v2-harnesses.md).

## 1. Goal

- **Any agent tool, any account.** A ticket's work can run on Claude Code, Codex or a local model (through OpenCode
  or similar), on a subscription, an API key or the office server. orch picks per task and falls back when an account
  is out of quota.
- **Helpers orch can see.** A main agent's sub-agents and the tasks orch itself starts on other tools appear as one
  tree, with state, a plain "now" line, cost and cache.
- **Jobs.** Work that agents and shells start (a Databricks run, an image model, dbt checks) shows up as a job with
  progress, steps and results, **without changing the team's existing skills**.
- **Live and later.** Focus mode shows it live; the ticket keeps a readable work log.

## 2. Decision: not herdr as the runtime

herdr (Rust, Apache-2.0, ~300k lines) is a terminal multiplexer for agents: a headless server owns the PTYs, a JSON
socket API (`agent.prompt`, `agent.wait`, `events.subscribe`), per-agent screen rules for blocked/working/idle, and
resume after a restart. We keep orch's runner:

- herdr's model is agents controlling each other through a socket handed to every pane (`HERDR_SOCKET_PATH`); orch's
  is the opposite (guard, grants, one-time terminal tickets, tier B under a separate UID).
- It knows nothing about tickets, epics, budgets or verdicts: that is the factory, and it stays orch code anyway.
- It does not answer permission prompts per tool, which #2 is built on.
- A pre-1.0 Rust binary in the signed installer, a second runtime next to the dashboard terminals, transcripts and
  phone views.

We **reuse its ideas**: the per-agent screen rules as data (#1854, Apache-2.0 with attribution) and resume via the
tool's own session id (`claude --resume`, `codex resume`).

## 3. What exists

| Piece | Where |
|---|---|
| Closed list of agent tools (`claude-code`, `codex`) and their command lines | `core/host/launch.py` |
| Runner: PTY, 1 MiB output ring, cgroup / birth-time kills, persisted-first spawn | `core/runner/supervise.py` |
| Transcripts and helper discovery for Claude and Codex | `core/runner/transcript.py`, `transcript_children.py` |
| Focus mode (Chat, Terminal, Changes, Decisions, Research, Tasks popover) | `dashboard/src/app/pages/focus` |
| "Maybe waiting" (Notification hook, else 90 s quiet) | `waiting_of()` in `core/host/runners.py` |
| Model choice by an add-on | `core/host/routing.py` (`choose_model`) |
| Claude's own sub-agents are switched off for orch agents | `--disallowedTools=Agent,Task` |

## 4. Agent tool adapters

Each entry of the closed list becomes an adapter (still reviewed code, never configuration):

| Field | Claude Code | Codex | OpenCode (new; local models) |
|---|---|---|---|
| command line, valid models | exists | exists | new |
| session id / resume | `--session-id`, `--resume` | rollout id, `codex resume` | `--session` |
| transcript and helpers | exists | exists | new |
| state rules (blocked / working / done) | #1854 | #1854 | #1854 |
| permission bridge | `PermissionRequest` hook (#2) | approval policy + sandbox mode | permission config |
| live events | hooks (§7) | transcript tail | transcript tail |
| usage (tokens, cache) | hooks / stream-json | `exec --json` | its log |
| installed and logged in? | check | check | check + server reachable |

- **Headless children.** A helper no person watches runs non-interactive (`claude -p --output-format stream-json`,
  `codex exec --json`): structured events, an exact end and usage, no screen reading. The interactive terminal stays
  for runs a person may take over.
- **No bridge, no prompts.** A tool without a permission bridge runs only inside the OS sandbox with deny by default.
  orch never "spots a dialog on screen and types yes".

## 5. AI accounts

A run is **agent tool × account × model**. Accounts: Claude subscription (`setup-token`), Anthropic API key, Codex
with a ChatGPT login, OpenAI API key, local server (URL plus a Test button). Keys live in orch's secret store and reach
a run as host-chosen environment values (§12.4.3 of the host spec). Setup gets an "Add AI account" step, no file
editing. Before automating several agents on one **subscription**, check the provider's terms.

## 6. Spawning and controlling helpers

- New ops: `orch spawn` (ticket, agent tool, account, mode; linked to its parent run), `orch run prompt`,
  `orch run wait --until done|blocked`, `orch run result`. Results come back through the ticket (a structured
  handoff), never by reading the screen.
- **Code decides, agents ask.** Agents file child tickets or ask for help; a fixed scheduler in orch starts runs
  within the charter's limits (depth, number, parallel runs per account, one claim per ticket). This is the
  runtime #2's factory loop sits on.
- **Routing and fallback.** `choose_model` grows into choosing tool, account and model by task kind, size and budget;
  a rate limit or exhausted quota moves to the next account.
- **Usage and budget.** Tokens and cost per account from hooks / JSON output / transcripts, plus each subscription's
  window. The charter budget counts these.

## 7. Live events (one shape for every tool)

```
event = { run, agent, kind, text, state, exit, ms, tokens }
kind ∈ tool.start · tool.end · command · file.edit · helper.start · helper.end · question · permission · state · usage
```

Sources, best first: (1) hooks (Claude Code `PreToolUse`, `PostToolUse`, `SubagentStop`, `Notification`, `Stop`);
(2) the JSON event stream of headless runs; (3) the transcript tail the runner already reads, now pushed when the file
changes instead of polled; (4) screen rules (#1854), for state only. The dashboard draws only these events, so a new
tool appears once its adapter exists. Commands are redacted before display (#1306 item 1 becomes a prerequisite).

## 8. Jobs: `orch.job/1`

### 8.1 Format

One JSON object per line, display only. Ids are shaped so they can map to OpenTelemetry spans later.

| kind | fields |
|---|---|
| `start` | `job`, `title`, `type`, `source`, `parent`, optional `total`, `unit` |
| `step` | `step`, `state` (queued, running, ok, failed, skipped), `ms` |
| `progress` | `done`, `total`, `unit`, optional `label` |
| `artifact` | `path` or `url`, `mime`, `preview` |
| `metric` | `name`, `value`, `unit` |
| `table` | `title`, `columns`, `rows` (max 50) |
| `link` | `label`, `url` (http/https only) |
| `log` | a few lines worth showing |
| `end` | `state` (ok, failed, cancelled), `summary` |

```json
{"v":1,"kind":"start","job":"dbx-551","type":"pipeline","source":"Databricks","title":"Nightly sales pipeline","total":5}
{"v":1,"kind":"step","job":"dbx-551","step":"transform_orders","state":"running"}
{"v":1,"kind":"metric","job":"dbx-551","name":"Cost so far","value":3.1,"unit":"DBU"}
{"v":1,"kind":"end","job":"dbx-551","state":"ok","summary":"5 tasks done, 4.2M rows, report table updated"}
```

### 8.2 How events get in: three layers, team skills untouched

1. **Hooks see every command and tool call** (deterministic, no cooperation). A team's Databricks skill ends in a
   shell command (`databricks jobs run-now …`) or an MCP call (`mcp__databricks__run_job`); orch sees both through
   its hooks (Claude) or the transcript (other tools). Any command over ~20 s becomes a job automatically (duration,
   exit code, last lines; new files in the ticket folder become its results).
2. **Job recipes** (data files, like herdr's rules), shipped by orch and by add-ons, or added by a team:
   ```toml
   id = "databricks-run"
   match.command  = '^databricks jobs (run-now|submit)\b'
   match.mcp_tool = 'databricks__run_job'
   extract.run_id = '"run_id":\s*(\d+)'
   extract.link   = '(https://[^\s"]+#job/\d+/run/\d+)'
   follow   = "databricks runs get --run-id {run_id} -o json"   # every 15 s until done; opt-in
   map.steps = ".tasks[] | {name: .task_key, state: .state.result_state}"
   ```
   Following runs in the agent's environment with the team's own login; orch never holds those credentials. It is
   opt-in per recipe and shown under Details.
3. **The `orch-jobs` skill** (in the orch plugin and AGENTS.orch.md): "when you start long work, report it with
   `orch job …` or lines to `$ORCH_EVENTS`". It only adds meaning hooks cannot see ("these 12 images are the product
   shots"). If an agent ignores it, layers 1 and 2 still show the job.

Ways to write events, all the same format: append to `$ORCH_EVENTS` (any language); the `orch job
start|step|progress|artifact|metric|table|end` CLI; or an escape sequence in normal output for jobs on another
machine over SSH or in a container (the runner already reads every terminal byte; the number is to be chosen).
`ORCH_JOB_PARENT` names the run; for Claude the job is matched to the shell command running at that moment (to verify:
whether the hook payload names the helper; else it attaches to the run).

### 8.3 Display only

Events are written by agents, so they are shown, never trusted: they cannot approve anything, create a "needs you"
card or open a link without a click. Paths stay inside the ticket's folder, links are http/https only, images are
size-capped and served through the existing artifact route.

## 9. Screens

Drafts: `~/orch-dev/mockups/focus-live/` (`index.html`, `details-on.html`, `ticket.html`); live mockup on branch
`proto/focus-live-ui` (demo ticket DEMO-0043). Every new part carries a Preview tag until it is real.

**Focus mode**
- Left: "Agents on this ticket": the main agent, helpers nested by depth, tasks orch started on other tools, and their
  jobs. Each row: state dot, name, written state, one "now" line. Finished rows fold.
- Middle: the selected agent's Progress (plain milestones; open one for the command and output), Chat, Terminal. A
  selected job shows its job view (progress, steps, metrics, images, failed-checks table, link to the tool's page).
- Right: a new Commands tab (scope this agent / all; status all, running, failed, waiting).
- Header: tasks done, "updated N s ago" (stale warning), "Stop ticket work"; "Stop this helper" on a helper.
- Requests: one banner "N requests need your review" and a row link, both opening one plain-words sheet ("Allow
  automatic code fixes?", what changes, why; the command under Technical details; Allow fixes / Decline).
- **Details switch, off by default.** On: model and account; cost as "Covered by subscription · API-price equivalent
  ≈ $x · not an extra charge", "Estimated API charges $x" or "Runs on your server"; account allowance once per
  account with its reset; conversation space; **cache** (Warm / Going cold in m:ss / Cold, with the cost of a cold
  start; Anthropic's cache lives 5 min after the last call, OpenAI's TTL is not published, so Codex shows an
  estimate); tokens.
- 720 px: the tree is a drawer behind "Agents ▾"; the selected agent stays on screen.

**Live work** (workspace page): every live ticket, needs-you first, with "now" and today's charges.

**Ticket**
- A new **Work** tab between Artifacts and History: summary (sessions, jobs, files, your decisions, charges), filters
  (Everything, Jobs and results, Your decisions, Problems), sessions grouped by day with milestones, job results and
  your decisions; "Open focus" or "Read the chat".
- A **Last work** card on Overview.
- History stays the signed record. Work is the readable story, unsigned and display only, kept with the ticket
  after the run ends (events capped per run; images kept only as artifacts). Sessions older than 30 days fold to one
  line per day.

## 10. Order

1. Adapter interface, headless JSON mode for helpers, #1854, redaction (#1306 item 1).
2. Live events (§7) pushed to focus mode; agent tree, Progress, Commands, Details with usage and cache.
3. `orch.job/1`: `$ORCH_EVENTS`, `orch job`, auto-jobs for long commands, the job view, the `orch-jobs` skill; then
   recipes (Databricks, dbt first) and following.
4. Ticket Work tab and Last work card.
5. AI accounts and OpenCode with a local model.
6. `orch spawn` / `run prompt` / `run wait`, routing and fallback, budget.
7. The factory loop (#2) on top.

Steps 1–4 make today's Claude and Codex runs visible; 5–7 make orch the multi-orchestrator.

## 11. Open questions

- Hook payloads: does a Claude `PreToolUse` for a helper's command identify the helper?
- Escape-sequence number for printed events, and whether to accept them from remote hosts at all.
- Retention of events and job images per ticket (default proposed: events capped per run, images only if kept).
- Price table source and update path for estimates; billed amounts only from provider billing data.
- Subscription terms for running several agents in parallel on one account.
