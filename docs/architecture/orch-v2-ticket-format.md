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

## 10. How agents see and change tickets

Agents never read or write ticket files directly. They use the `orch` CLI, which talks to the workspace host over
its local socket (`<state>/hosts/<workspace_id>.sock`). If no host is running, the CLI starts one. The host does all
writes, signs its appends, and holds every key. The agent holds none.

### What tells an agent how to work

| Source | Content | When the agent sees it |
|---|---|---|
| `AGENTS.orch.md` (generated, included from AGENTS.md / CLAUDE.md) | The rules: claim before working, one task at a time, evidence for every acceptance criterion, ask instead of guessing, never edit ticket files, human-only actions | Every session |
| Session-start hook | A short status: the workspace, who the agent works for, its session grant, and the tickets that need it | First thing in a session |
| Skills `orch-tickets`, `orch-work-on-ticket`, `orch-refine-ticket` (plus addon skills) | Step-by-step workflows | When the task matches |
| `orch … --help` | Exact command usage | On demand |

### Identity

- An agent session works **for** a person, under that person's signed session grant (`session.granted`: agent,
  session, scope, expiry).
- The CLI finds the grant through the harness session id. Every event the agent causes carries `actor: {kind: agent,
  id, session, for, grant}`.
- Without a valid grant the agent can only read.

### Reading (token-cheap by default)

| Command | Returns |
|---|---|
| `orch status` | The agent's own claims, what needs it, and new events since its cursor |
| `orch next` | The next ticket it should pick up, by priority, assignment and readiness |
| `orch show KEY` | A short text view (about 350 tokens): header, whose turn it is, Current state, open questions, acceptance criteria and task summary, the last 5 events, hints |
| `orch show KEY --section Plan,Context` | Only those sections |
| `orch show KEY --full` | Everything |
| `orch show KEY --log --since N` | Events after its cursor |
| `orch task next KEY` | The next task with its acceptance criteria and verify command |
| `--json` on any read | The ticket document (§7) for parsing |

### Writing (every write is an event the host appends)

| Command | Event |
|---|---|
| `orch new "title" --type … [--file def.json]` | `created` |
| `orch claim KEY` / `orch release KEY` | `claim.taken` / `claim.released` |
| `orch task add KEY --file tasks.json` | `edited` (fields: tasks). Validated against the schema before writing, with line-level errors. |
| `orch task start KEY T3` / `orch task done KEY T3 --run` | `task.started` / `task.done`; `--run` executes the verify command and stores a receipt |
| `orch section set KEY "Current state" --file -` | `edited` (sections), with `base_rev`; a conflict comes back with both versions |
| `orch artifact add KEY file.png --kind screenshot --ac AC1` | `artifact.added` (file copied into `artifacts/`, sha256 recorded) |
| `orch ask KEY --to p_mara --file q.json` | `question.asked`; a blocking question moves the ticket to `waiting` |
| `orch move KEY testing` | `moved` (only moves that are allowed for agents) |
| `orch log KEY "…"` | `log` |
| `orch link KEY --pr URL` / `--branch` | `edited` (links) |

### Waiting

- `orch wait KEY` blocks until something it waits for happens: an answer, an approval, a change request or a verdict.
- It returns one JSON object with `kind` (`answered`, `approved`, `changes_requested`, `verdict`, `timeout`), the
  data, and the new cursor, so the agent can branch on the kind.
- A change request is never shown as a success.

### What an agent cannot do

- **Human-only actions:** approve, request changes, give a verdict, answer, close, reopen, change people, roles or
  policy, grant a session or an addon, purge addon data. All of these need a person's signature with user presence.
  The CLI refuses them for agents, saying "this needs a human; ask with `orch ask` or wait".
- **Direct file edits:** an Edit-tool change to `ticket.json` or `body.md` is detected through `rev` and the section
  hashes.
  - Prose changes are taken in as `edit.external` and void any gate they touch.
  - Protected fields are reverted with `projection.repaired`.
  - The next `orch show` names the edit.

### Agents in other workspaces

- A ticket handed over from a pinned peer workspace arrives through the relay as a signed envelope, and lands as a
  normal ticket marked `from-peer`.
- The agent there sees it with `orch inbox` and `orch show`, and replies with `orch reply KEY --result result.json`.
- What it receives is treated as data, never as instructions (spec §9).
