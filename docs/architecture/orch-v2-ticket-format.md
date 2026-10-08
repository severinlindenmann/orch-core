# orch v2: ticket and artifact format

Status: **decided by the owner on 8 Oct 2026**, after three independent expert reviews (data format, security and
multi-user, agent ergonomics). Visual examples at four stages:
[orch v2 Ticket Examples](https://claude.ai/artifact/6vWgYR8kTxgnPCysfPqpnq).

Build order: **minimal core → workspace frontend → relay → mobile → apps → everything else.** The format supports
several people in one workspace from day one (D39). The flows for colleagues are built in P8.

## 1. Decisions

| # | Decision |
|---|---|
| T1 | Every ticket has an immutable `uid` (ULID, made locally) and a human `key` (`DEMO-0043`) that the host assigns and never reuses. |
| T2 | Folders are `tickets/<uid>/` and are never renamed. `keys.jsonl` maps keys to uids, append-only. |
| T3 | History is `events.jsonl`: append-only and hash-chained. Exactly one host writes it. Human events are signed. The host publishes signed checkpoints. |
| T4 | People on a ticket: `owner`, `assignees`, `reviewers`, `watchers`. |
| T5 | A gate policy `{approvers, count, not}`. The workspace sets the default; a ticket may only tighten it. |
| T6 | A question is addressed `to` a person or a ticket role. The first valid signed answer for the current hash wins. |
| T7 | A claim is one agent session working `for` a person, backed by a session grant that person signed. Claims expire; a takeover is logged. |
| T8 | A signed member list with the roles owner, maintainer, member and viewer. |
| T9 | Only the host writes. Edits carry `base_rev` and merge per section; a conflict on the same section is returned to the editor. A direct file edit becomes `edit.external`. |
| T10 | `ticket.json` holds the structured data (fields, people, links, acceptance criteria, task definitions, questions, addons). `body.md` holds the prose. **There is no YAML.** |
| T11 | Addon data lives only under `addons.<name>`, and addon events are named `<name>.*`. A manifest field's `set_by` names roles. |
| T12 | A restricted ticket is enforced by the host and sealed in transit over the relay. It stays plain on disk. Real secrecy means a separate workspace. |
| T13 | Everything optional is an addon. Addons run out of process, with capabilities granted in signed events. |
| T14 | **Events are the only truth for state:** status, people, claims, task and acceptance-criteria state, answers, gates, and artifacts. |
| T15 | The prose sections depend on the ticket type. Context and Decisions are added; Current state is capped. |
| T16 | Every human signature on a Mac needs user presence (Touch ID or password) until agents run under their own OS user (P1b). |

## 2. Workspace layout

```
orchestrator/
├── config.json              # workspace id, host id, members, gate defaults, addons
├── keys.jsonl               # DEMO-0043 -> uid, append-only, never reused
├── AGENTS.orch.md           # generated rules for agents
├── events/workspace.jsonl   # signed workspace events: members, roles, policies, addon grants
├── tickets/
│   └── 01J9ZK4Q7M3R8T2V6X0B5N1C9D/
│       ├── ticket.json      # structured definition (authored)
│       ├── body.md          # prose sections (authored)
│       ├── events.jsonl     # history and all state
│       └── artifacts/       # evidence files (the manifest comes from artifact events)
└── .state/                  # git-ignored: index.sqlite (derived state), locks, sessions
```

`config.json` (excerpt):

```json
{
  "schema": "orch.workspace/2",
  "workspace": {"id": "6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11", "prefix": "DEMO", "name": "Acme energy data"},
  "host": {"id": "h_01J9Z7", "wsk_pub": "ed25519:…"},
  "members": [
    {"person": "p_sev", "name": "Severin", "role": "owner"},
    {"person": "p_mara", "name": "Mara", "role": "maintainer"},
    {"person": "p_tom", "name": "Tom", "role": "viewer"}
  ],
  "gates": {
    "requirements": {"approvers": "owner", "count": 1},
    "plan": {"approvers": "owner", "count": 1},
    "verify": {"approvers": "reviewers", "count": 1, "not": "assignees"}
  },
  "agents": {"run_for": ["owner", "maintainer", "member"]},
  "addons": {"dashboard": {"enabled": true}, "estimate": {"enabled": true}, "publish": {"enabled": false}}
}
```

- `config.json` mirrors what the signed events in `events/workspace.jsonl` say (`member.added`, `role.changed`,
  `policy.changed`, `addon.granted`).
- When the two disagree, the events win, and the host rewrites the file and logs `projection.repaired`.

## 3. `ticket.json`

Pretty-printed with a fixed key order, so git diffs show one value per line. It is validated against JSON Schema
2020-12, and duplicate keys are refused. It holds **definitions only, never state**.

```json
{
  "schema": "orch.ticket/2",
  "uid": "01J9ZK4Q7M3R8T2V6X0B5N1C9D",
  "key": "DEMO-0043",
  "title": "Load tariff tables as dbt seeds",
  "type": "feature",
  "priority": "high",
  "size": "m",
  "labels": ["dbt", "tariffs"],
  "parent": "DEMO-0040",
  "blocked_by": [],
  "due": null,
  "visibility": "workspace",
  "links": {
    "repos": ["acme-energy-dbt"],
    "branches": {"acme-energy-dbt": "feat/DEMO-0043-tariff-seeds"},
    "prs": [{"repo": "acme-energy-dbt", "url": "https://github.com/acme/energy-dbt/pull/31"}],
    "external": []
  },
  "acceptance": [
    {"id": "AC1", "text": "`dbt seed` loads all 40 tariff tables without errors"},
    {"id": "AC2", "text": "Model `fct_billing` joins the seeds and its tests pass"},
    {"id": "AC3", "text": "The refresh command is documented in README"}
  ],
  "tasks": [
    {"id": "T1", "text": "Export CSVs into seeds/tariffs", "verify": {"cmd": "ls seeds/tariffs | wc -l"}, "proves": []},
    {"id": "T2", "text": "Seed configs with types", "verify": {"cmd": "dbt seed --select tariffs"}, "proves": ["AC1"]},
    {"id": "T3", "text": "Join in fct_billing, add tests", "verify": {"cmd": "dbt test --select fct_billing"}, "proves": ["AC2"], "assignee": "p_sev"},
    {"id": "T4", "text": "Document the refresh command", "verify": null, "proves": ["AC3"], "assignee": "p_mara"}
  ],
  "questions": [
    {
      "id": "Q1",
      "to": "p_mara",
      "text": "Which tariff export is the source of truth, the monthly CSV or the API?",
      "why": "The two differ for 3 of 40 tariffs; the seed must pick one.",
      "options": [{"key": "csv", "label": "Monthly CSV"}, {"key": "api", "label": "Tariff API", "cost": "+1 day"}],
      "recommended": "csv",
      "blocking": true
    }
  ],
  "addons": {"estimate": {"points": 5}}
}
```

- **Not in this file:** `status`, `people`, the claim, task state, acceptance-criteria state, answers, gate state and
  the artifact list. All of these come from events. The ticket document (§7) shows them derived.
- **ids:** the host assigns `AC`, `T` and `Q` ids and never reuses or renumbers them.
- **Text rules:** NFC, LF line endings, RFC 3339 UTC timestamps with second precision, and no floats in anything that
  is signed (use `ms`, `cents`).

## 4. `body.md`

Prose only, with no frontmatter. A section starts with `## ` at column 0 outside a code fence, as in v1, including
v1's guards against forged headings.

| Section | feature | bug | chore | spike / investigation | epic |
|---|---|---|---|---|---|
| Summary | optional | optional | optional | optional | yes |
| Context (code, files, related tickets) | yes | yes | optional | yes | yes |
| Requirements | yes | yes | yes | yes (questions to answer) | yes |
| Out of scope | yes | yes | — | — | yes |
| Plan | yes | yes | yes | yes | — |
| Decisions (chosen, rejected, why) | yes | yes | optional | yes | yes |
| Verification | yes | yes | — | findings | — |
| Current state (handoff, at most about 10 lines, rewritten) | yes | yes | yes | yes | yes |

Limits: 64 KB per section and 256 KB per `ticket.json`.

## 5. `events.jsonl`

One JSON object per line. Each event has:
- `v`, `id` (ULID), `seq`, `at` (the host's clock);
- `type`, and an `actor`, either `{kind: person, id, device}` or `{kind: agent, id, session, for, grant}`;
- `based_on` (the head the actor saw), `prev` (the head when the host appended it) and `hash_v`;
- `sig` for human events, and `host_sig` (WSK) on every appended event.

```json
{"v":2,"id":"01J9ZP…","seq":5,"at":"2026-10-09T09:10:11Z","type":"gate.approved","gate":"requirements","hash":"sha256:fa37…","hash_v":1,"policy_hash":"sha256:31c2…","list_seq":7,"actor":{"kind":"person","id":"p_sev","device":"d_mac"},"presence":"touchid","based_on":"sha256:aa91…","prev":"sha256:aa91…","sig":"ed25519:…","host_sig":"ed25519:…"}
{"v":2,"id":"01J9ZQ…","seq":9,"at":"2026-10-09T10:40:22Z","type":"task.done","task":"T2","receipt":{"exit":0,"ms":38200,"commit":"b7e1f02"},"actor":{"kind":"agent","id":"claude-code","session":"s_77c2","for":"p_sev","grant":"gr_01J9…"},"based_on":"sha256:51e0…","prev":"sha256:51e0…","host_sig":"ed25519:…"}
{"v":2,"id":"01J9ZR…","seq":10,"at":"2026-10-09T10:41:03Z","type":"artifact.added","name":"seeds-in-warehouse.png","kind":"screenshot","sha256":"3f9a…","bytes":84213,"ac":"AC1","actor":{"kind":"agent","id":"claude-code","session":"s_77c2","for":"p_sev","grant":"gr_01J9…"},"based_on":"sha256:98ac…","prev":"sha256:98ac…","host_sig":"ed25519:…"}
```

Rules:

- **Signing.** A human signs `cj(event without seq, at, prev, host_sig)` with the context
  `orch/v2/sig/ticket-event|<workspace_id>|<uid>`. The signature covers `based_on` and `list_seq`, so it survives
  interleaving and can't be replayed into another ticket. Signed human events: gate approvals, change requests,
  verdicts, answers, close, reopen, changes to people, roles or policy, `set_by` human addon fields, session grants
  and addon purge.
- **Checkpoints.** After appends, the host publishes WSK-signed checkpoints `{uid, seq, head}` and a workspace
  checkpoint over all ticket heads, to the relay and to member devices. A lower `seq` is refused. A rollback (a
  restored backup, a git force-push) needs an owner-signed `restore {from_seq, reason}`.
- **Order** is by `seq` only.
- **Gate hash** = `sha256(JCS({workspace_id, uid, gate, schema, hash_v, sections: {name: normalised text}, fields:
  {type, size, acceptance, gated addon fields}, tasks (plan gate: ids, text, verify, proves), artifacts: {name:
  sha256 shown inline}, policy_hash, people_hash}))`.
- **Who may approve.** An approval counts only if the approver:
  - is a current member with an eligible role;
  - was not an assignee since the gated content last changed;
  - had no agent edit that content on their behalf;
  - signed the current `policy_hash`.
- **Removing a member** voids their unused approvals, releases their agents' claims and re-routes their questions.
  Their earlier signatures stay valid against the `list_seq` they cite.
- **Unsigned file edits.** A `ticket.json` or `body.md` change the host didn't write becomes `edit.external`
  (unattributed, never signed). It may change prose only, and it voids any gate whose hash it changes. Changes to
  protected fields are reverted with `projection.repaired`.

## 6. Artifacts

Files live in `artifacts/`. The manifest comes from `artifact.added` and `artifact.replaced` events (name, kind,
sha256, bytes, task, ac, label, actor). There is no separate manifest file that gets rewritten.

- **Core kinds:** screenshot, log, report, link, dataset, build, diagram, receipt (from `task.done --run`), feedback
  (only a human may add it), other.
- **Addon kinds** are declared in the manifest. An addon artifact has `addon` + `ref` instead of a file.
- **Inline use in prose:** `![alt](artifact:after.png)`. A gated section that shows an artifact binds its sha256.

## 7. The ticket document (`orch show --json`)

What the dashboard, the relay and the phone read: `ticket.json` merged with state derived from events, and cached in
`.state/index.sqlite`, which can be rebuilt at any time. The [examples page](https://claude.ai/artifact/6vWgYR8kTxgnPCysfPqpnq)
shows it at four stages.

For agents, `orch show <key>` prints a short text view by default (about 350 tokens):
- header and whose turn it is;
- Current state;
- open questions;
- acceptance-criteria and task summary;
- the last 5 events.

Other views: `--full`, `--section Plan,Context`, `--log --since <seq>`, `orch task next <key>`, and `--json`.

## 8. Addons

An addon declares in its manifest (`orch-addon.json`):

| Key | What it adds |
|---|---|
| `fields` | Stored under `addons.<name>` in `ticket.json`. Each field has a type, an enum, `set_by` (roles, `agent` or `addon`), `gate`, and `filter` / `show`. |
| `sections` | Extra `body.md` sections, with placement, gate membership and ticket types. |
| `artifact_kinds` | Extra artifact kinds. |
| `needs` | "Needs you" rules in a small expression language. |
| `cli`, `skills`, `agents_md` | A command group, skills and text for `AGENTS.orch.md`. |
| `capabilities` | What the addon may do, e.g. `serve_http`, `spawn_agent`, `pty`, `network`. |

Rules:

- **Addons run out of process.** The owner grants capabilities in a signed `addon.granted {name, version,
  package_sha256, capabilities}`, and every update needs a new grant.
- **Only core shows and signs approval prompts.** An addon UI asks core to do it.
- **`pty` (terminals) is never granted to agents.**
- **A disabled addon's data stays untouched** and is shown as inactive.
- **A manifest change to gated fields** changes the gate hash.

The first-party addons (dashboard, terminals, worktrees, quick tasks, widgets, records, activity, start agent,
publish, github, estimate, usage, wiki) live in the orch-core repo under `addons/` and use the same API as every
other addon.

## 9. Visibility

`"visibility": "workspace"` (the default) or `{"restricted": ["p_sev", "p_mara"]}`.

- **Host:** the CLI, the dashboard and the relay hide a restricted ticket from other members (`not_visible`).
- **Relay:** its bodies are sealed to the listed people's devices.
- **Disk:** it stays plain, readable by whoever has the files.

Real secrecy from other members means a separate workspace.

## 10. The agent interface

The `orch` CLI is built for agents first. The goal: an agent does its work with **as little extra context as
possible**. It loads nothing it doesn't need, its output is terse, and it is told what to do next.

### 10.1 Decisions

| # | Decision |
|---|---|
| A1 | **The CLI is primary.** There is no MCP server in the core. Every command is defined once, in an operation registry (one JSON Schema per operation), and the CLI, `orch describe`, `orch help` and a later MCP addon are all generated from it. |
| A2 | **No daemon on day one.** The CLI runs the registry in-process and writes through one `Store.append` behind a file lock. In P1 it plays the host's role: it holds the workspace key, signs appends, and enforces gates, roles, policies and visibility. When the dashboard or relay arrive, the same registry moves into the host behind the socket, and commands and output stay the same. |
| A3 | **Standing grants.** `orch grant [--hours 8]` (one Touch ID) signs a grant for the person, the workspace and every verb that isn't human-only. Every session and subagent uses it through `ORCH_GRANT`, and CI gets its own narrower grant. Without a grant, only the low-risk writes `ask`, `log` and `artifact add` work, and they are marked `unattended`. Human-only actions need Touch ID or a passkey every time. |
| A4 | **Parallel subagents.** A ticket has one claim (the manager's). Subagents run with `ORCH_SESSION=<parent>.<sub>` and take a **lease** on one task with `orch task start T3`. Two sessions on the same task are refused. |
| A5 | **All format behaviour (T1–T16) is in P1.** Checkpoints are written locally and published once the relay exists (P2). |

### 10.2 What the agent is given (context budget)

| Source | Size | Loaded |
|---|---|---|
| `AGENTS.orch.md` | at most 25 lines, with a version stamp | every session |
| Session-start hook | 3–6 lines: who the agent works for, its grant, what needs it. Re-injected after context compaction. | every session start or compaction |
| Skills `orch-tickets`, `orch-work-on-ticket`, `orch-refine-ticket` | short, judgment rules only (when to ask, what a good plan is) | only when the task matches |
| `orch help <workflow>`, `orch describe <cmd>` | generated from the registry, so never stale | only when the agent asks |
| Addons | one line each in `AGENTS.orch.md`; details through `orch describe` | on demand |

`AGENTS.orch.md`, the whole file:

```
orch v2.0 · tickets only through `orch`, never edit tickets/**
start: orch status
work:  orch claim --next | orch task next | orch task done T3 --run
prove: every AC needs evidence (receipt, or orch artifact add --ac AC1)
unsure? orch ask "…" --options a,b --rec a   then: orch wait
handoff: orch handoff -m "…"    finished: orch submit
parallel subagents: ORCH_SESSION=<yours>.<n>; each takes one task with orch task start
refused with retry:false → stop and tell the user
more: orch help work · orch describe <cmd>
```

A stale check at session start compares the version stamp with the installed CLI. When they differ, it prints one
line: `instructions stale: run orch instructions sync`.

### 10.3 Commands

A reference (`REF`) is `DEMO-0043`, `43`, `DEMO-0043/T3`, or just `T3`. **Leaving it out means "my current
claim"**, if the session holds exactly one; otherwise `ambiguous_ref` comes back with the candidates.

| Group | Commands |
|---|---|
| Context | `status` (also shows who the agent is, its grant and its cursor), `describe [cmd]`, `help <workflow>` |
| Read | `show [REF] [--section A,B \| --full \| --log --since N \| --diff --since N]`, `list`, `search`, `next`, `inbox` |
| Lifecycle | `new`, `claim [REF \| --next \| --takeover --reason]`, `release`, `handoff -m`, `submit`, `ask "…" --options a,b --rec a [--to p]`, `wait` |
| Edit | `set REF key=value` (title, priority, labels, due, links), `section set`, `ac add\|edit`, `task list\|next\|add\|start\|done\|skip\|block\|reopen`, `artifact add\|replace\|list`, `log`, `apply --file -` (an atomic batch) |
| Human only | `approve`, `request-changes`, `verdict`, `answer`, `close`, `reopen`, `grant`, `member`. Agents get `human_only`, `retry:false`. |
| Admin | `init`, `doctor`, `check`, `instructions sync`, `import v1`, `addon …` |

Combined calls for the common loops:

- `orch task done T3 --run --artifact out.png --ac AC2 -m "…"` runs the check, stores the receipt and the evidence,
  and prints the next task.
- `orch submit` moves the ticket to testing only when every acceptance criterion has evidence. Otherwise it lists
  what's missing.

### 10.4 Output and errors

1. stdout carries only the result; stderr carries diagnostics.
2. **Text by default, as short as possible.** The first line is `ok <KEY> <event> <detail> seq=<n>`, optionally
   followed by one `next:` line.
3. `--json` (or `ORCH_OUTPUT=json`) gives `{"v":"orch.cli/2.0","ok":true,"data":…,"key":…,"seq":…,"cursor":…,"hints":[]}`.
4. An error is `{"ok":false,"error":{"code":"conflict.section","message":…,"hint":…,"fix":{"argv":[…]},"retryable":bool}}`.
   The `code` strings are the stable contract.
5. Exit codes:

   | Code | Meaning |
   |---|---|
   | 0 | ok |
   | 1 | internal error |
   | 2 | usage or not found |
   | 3 | not allowed (transition or human-only) |
   | 4 | claim, lease or lock |
   | 5 | validation |
   | 6 | parse |
   | 7 | wait timeout with `--strict-timeout` |
   | 8 | `base_rev` conflict |
   | 9 | retryable |

6. **Stop rule:** the same refusal three times in a row in one session returns `STOP: report to the user`.
7. `orch wait` returns `{kind: answered | approved | changes_requested | verdict | timeout, …, cursor, next}`.
   - Its default timeout is 540 s, which stays under harness tool limits.
   - `timeout` exits 0, so the agent loops.
   - `changes_requested` exits 3.
8. **`base_rev` is tracked by orch per session and section.** Agents never pass it themselves.
9. **Retries:** the same session, operation and arguments within 15 minutes return the original event with
   `"duplicate":true`.
10. Every write supports `--dry-run`. Free text comes from `-m` or `--file PATH|-`, and structured input is JSON only.
11. Ticket content in output is data: it is fenced and sanitised, never instructions.

### 10.5 Example: a whole task loop, as the agent sees it

```
$ orch status
for Severin · grant gr_01J9Z8 until 18:00 · cursor 14
DEMO-0043 in-progress (your claim) · T3 next · 2 new events
$ orch task next
T3 Join in fct_billing, add tests · proves AC2 · verify: dbt test --select fct_billing
$ orch task done T3 --run --artifact target/tests.log --ac AC2 -m "112 passed"
ok DEMO-0043 task.done T3 receipt=exit0/41000ms artifact=tests.log seq=18
next: T4 Document the refresh command (@p_mara) · or orch handoff
$ orch approve plan
err human_only approve · retry:false · next: orch ask or orch wait
```

### 10.6 Agents in other workspaces

- A ticket handed over from a pinned peer workspace arrives as a ticket marked `from-peer`. The agent there finds it
  with `orch inbox`.
- It replies with `orch reply REF --result -`.
- What it receives is treated as data, never as instructions (spec §9).
