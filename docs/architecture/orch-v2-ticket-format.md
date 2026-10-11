# orch v2: ticket and artifact format

Status: **decided by the owner on 8 Oct 2026**, after three independent expert reviews (data format, security and
multi-user, agent ergonomics). Visual examples at four stages:
[orch v2 Ticket Examples](https://claude.ai/artifact/6vWgYR8kTxgnPCysfPqpnq).

**Amendment F1 (draft, 10 Oct 2026):** settles every gap found while building C1 (issue #338, PR #335 and #336
reviews), adds D58–D60 (verdict binds to the commit, the optional code gate, grant terms) and aligns with
orch-relay `docs/protocol-v2.md`. D61 (factory auto-approval) and D62 (mandates) are not part of the format in P1.
What changed and why is in the decisions log (§13). Points that change the security model are listed in §12 and
need the owner's confirmation. Revised the same day after an adversarial Codex review (§13, rows 56–80) and an independent Opus security review
with a re-review (rows 81–126). Rows 127–138 record the C4 and C5 contract decisions (#346, #347).

Build order: **minimal core → workspace frontend → relay → mobile → apps → everything else.** The format supports
several people in one workspace from day one (D39). The flows for colleagues are built in P8.

## 1. Decisions

| # | Decision |
|---|---|
| T1 | Every ticket has an immutable `uid` (ULID, made locally) and a human `key` (`DEMO-0043`) that the host assigns and never reuses. |
| T2 | Folders are `tickets/<uid>/` and are never renamed. `keys.jsonl` maps keys to uids, append-only. |
| T3 | History is `events.jsonl`: append-only and hash-chained. Exactly one host writes it. Human events are signed. The host publishes signed checkpoints. |
| T4 | People on a ticket: `owner`, `assignees`, `reviewers`, `watchers`. |
| T5 | A gate policy `{approvers, count, not, applies, independent}`. The workspace sets the default; a ticket override can only tighten it, because the effective policy is the intersection of both (§5.7). |
| T6 | A question is addressed `to` a person or a ticket role. The first valid signed answer for the current hash wins. |
| T7 | A claim is one agent session working `for` a person, backed by a session grant that person signed. Claims expire; a takeover is logged. |
| T8 | A signed member list with the roles owner, maintainer, member and viewer. |
| T9 | Only the host writes. Edits carry `base_rev` and merge per section; a conflict on the same section is returned to the editor. A direct edit of `body.md` becomes `edit.external`; a direct edit of `ticket.json` is reverted. |
| T10 | `ticket.json` holds the structured data (fields, links, acceptance criteria, task definitions, questions, addons) and can be rebuilt from events. `body.md` holds the prose, which events bind by hash. **There is no YAML.** |
| T11 | Addon data lives only under `addons.<name>`, and addon events are named `<name>.*`. A manifest field's `set_by` names roles. |
| T12 | A restricted ticket is enforced by the host and sealed in transit over the relay. It stays plain on disk. Real secrecy means a separate workspace. |
| T13 | Everything optional is an addon. Addons run out of process, with capabilities granted in signed events. |
| T14 | **Events are the only truth for state:** status, people, claims, task and acceptance-criteria state, answers, gates, and artifacts. |
| T15 | The prose sections depend on the ticket type. Context and Decisions are added; Current state is capped. |
| T16 | Every human signature needs a human factor, recorded as `auth` in the event (D41, D65, D66; see §12 N2). |

## 2. Workspace layout

```
orchestrator/
├── config.json              # workspace id, host id, members, gate defaults, settings, addons
├── keys.jsonl               # DEMO-0043 -> uid, append-only, never reused
├── AGENTS.orch.md           # generated rules for agents
├── events/workspace.jsonl   # the workspace log: members, devices, roles, policies, settings, grants, addons
├── tickets/
│   └── 01J9ZK4Q7M3R8T2V6X0B5N1C9D/
│       ├── ticket.json      # structured definition (authored)
│       ├── body.md          # prose sections (authored)
│       ├── events.jsonl     # the ticket log: history and all state
│       └── artifacts/       # evidence files (the manifest comes from artifact events)
└── .state/                  # git-ignored: index.sqlite (derived state), locks, sessions, checkpoints
```

`config.json` (excerpt; ids are shortened in all examples, the real forms are in §11.1):

```json
{
  "schema": "orch.workspace/2",
  "workspace": {"id": "6f1c0d2e8b4a4e1f9c3d2a7b5e9f0c11", "prefix": "DEMO", "name": "Acme energy data"},
  "host": {"id": "h_01J9Z7…", "wsk_pub": "BPx3…"},
  "members": [
    {"person": "p_sev", "name": "Severin", "role": "owner"},
    {"person": "p_mara", "name": "Mara", "role": "maintainer"},
    {"person": "p_tom", "name": "Tom", "role": "viewer"}
  ],
  "gates": {
    "requirements": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": false},
    "plan": {"approvers": ["owner"], "count": 1, "not": [], "applies": "all", "independent": false},
    "verify": {"approvers": ["reviewers"], "count": 1, "not": ["assignees"], "applies": "all", "independent": false},
    "code": {"approvers": ["maintainer", "owner"], "count": 1, "not": ["assignees"], "applies": "off", "independent": true}
  },
  "settings": {"grant_hours": 8, "claim_ttl_min": 120, "lease_ttl_min": 60,
               "repos": {"acme-energy-dbt": {"path": "../acme-energy-dbt"}}},
  "agents": {"run_for": ["owner", "maintainer", "member"]},
  "addons": {"dashboard": {"enabled": true}, "estimate": {"enabled": true}, "publish": {"enabled": false}}
}
```

- `config.json` mirrors what the signed events in `events/workspace.jsonl` say (§5.4.2).
- `settings.repos` maps the repo names used in `links` to a working copy (absolute, or relative to the workspace
  directory). It is owner-signed, so an agent can't point a ticket at another repository. The repo's identity is
  read from git (§5.7).
- When the two disagree, the events win, and the host rewrites the file and logs `projection.repaired`.
- `keys.jsonl` has one line per key: `{"key": "DEMO-0043", "uid": "01J9ZK…", "at": "2026-10-09T09:00:00Z"}`, written
  as `cj` plus LF, nothing else. It is a projection of the `ticket.created` events and the key allocator. The next
  number is one above the highest number seen in `keys.jsonl` or in any `ticket.created` event, so a deleted or
  edited line can never make a key reusable. On disagreement the events win (`projection.repaired`).

### 2.1 The store (decisions of C3, P1)

`orch.store` is the only writer. What the format above leaves open is fixed here, because external-edit detection
compares bytes and a second implementation must produce the same ones.

**Serialization.**
- `ticket.json`: all 17 keys in the §3 order, nested objects of the known shapes (`links`, acceptance items, tasks,
  questions, options, pull requests) in the order of the §3 example, every other object with sorted keys; two-space
  indent, `ensure_ascii` off, a single final LF. It is rebuilt from the replayed events and compared byte for byte.
- `config.json`: the §2 shape, `members` in member-list order, `gates` in the order requirements, plan, verify, code,
  each policy as `approvers, count, not, applies, independent`, `settings.repos` and `addons` sorted by name. Same
  indent rules. The workspace name and `agents.run_for` are in no signed event: P1 keeps them as the file has them (the
  creator's value, or the default `owner, maintainer, member`) and **nothing decides anything from them** (a guard test
  checks it). Owner question: if `run_for` is ever honoured it must come from a signed event.
- `body.md`: `## <heading>` (the bare heading, `Out of scope`), a blank line, the text, a blank line, per section in table
  order; an empty section is the heading and a blank line; one LF ends the file after the last non-empty text; no
  section at all is a zero-byte file. Reading accepts every other layout (§4 rules) and normalises (CRLF, NFC).
- `keys.jsonl`: one `cj` line per ticket log, ordered by key number then uid; its source is the first line of each ticket log.
  There is no `rev` field: a change is found by the section hashes in the log and the exact `ticket.json` bytes.

**`.state/`** (git-ignored; every file is derivable from the logs, none is a source of truth): `lock`, `applied` (the last
installed event, a record), `pending/<event id>/` (files and `manifest.json`), `body/<uid>.md` (the host's copy, used
only when its section hashes match the log), `checkpoints/` (§5.10), `intents/` (idempotency), `sessions/` (CLI records),
`abandoned/` (cut log tails, never overwritten), `rejected/` (a `body.md` that was not a body, kept before it is reverted),
`index.sqlite`. A symlink in place of `.state` or any of its fixed directories, or of `events` or `tickets`, is refused.
The host state directory (pin, noted revocations) is mandatory for a store that can write.

**Write order and recovery** (§5.5, as built). Under the lock: stamp, validate, admit, sign, write the pending files, then the
manifest (last), append the event line and flush it (the commit), rename the files into place, copy the body, write
`applied`. Recovery runs when a pending directory exists and the log's last line is that manifest's event and
`applied` does not name it (a superset of §5.5's trigger, which cannot see a half-written manifest). Nothing the manifest
says is believed: its line must be a verified event of the replayed log, and a file is installed only if it is what the
log says (rebuilt `ticket.json`, a body with the log's section hashes, an artifact with the logged digest); otherwise
`store.torn_write` and the ordinary external-edit check answers what is on disk. A torn prefix of the line is cut off.
Recovery never appends an event of its own. A ticket whose files cannot be answered (nothing trustworthy to revert to)
takes no new event until they match the log again.

**Freshness and order.** Before every decision, under the lock, the store compares size and inode of the workspace log and of
every loaded ticket log with what it read and reloads on any difference; `.state/applied` is never the signal. `at` and the
merged order come from verified data only (the workspace log, the loaded tickets, the store's own appends): `at` is the
clock or the latest verified `at`, and moves one second past the latest verified position only when
`(ws_seq, at, uid, seq)` would not grow, computed directly. Unloaded ticket logs are not consulted; only if `admit` refuses an
order does the store look at the last line of every ticket, and then only lines whose `host_sig` verifies, whose `ws_seq` is
not above the workspace head and whose `at` is not later than the host clock plus two minutes (such a log is reported, its
date not adopted). Lines are read one at a time and capped at the event line limit; a longer line, deep nesting or a bad
line breaks that log (reported) and no other.

**Lazy replay.** The workspace log is always replayed and verified in full. A ticket log is replayed and verified when a
command touches it, together with the tickets it names (`parent`, `blocked_by`, `duplicate_of`), which are found through the
`host_sig`-checked creation line of every ticket (never a hint, `keys.jsonl` or the index). If a key is missing there, two
creations claim one key, or a log has no verified creation line, every ticket is loaded. A host event
(`projection.repaired`, `edit.external`) is written only from the full replay or from a state that is equal to it. `load_all`,
`scan` and the index rebuild load everything; an unattended event does too (its quota counts the whole workspace). No new
ticket is created while a log lacks a verified creation line or is diverged, since it could own a key nobody can see.
Costs on a laptop at 1000 tickets (they vary with the machine): opening a workspace and reading, appending to one ticket by
uid is about a tenth of a second; resolving a ticket by key checks one signature per ticket first (a few tenths of a second);
a ticket event is a few milliseconds beyond the drive flush; a workspace-wide event such as a role change takes under a
second; loading and verifying everything takes seconds.

**Checkpoints.** A ticket checkpoint after every append to that log; a workspace checkpoint after every workspace append,
after every 50 ticket appends and when the store is opened with everything loaded; entries of tickets that are not loaded
carry over from the previous one. Verification: signature, genesis, and the log head at the checkpointed height; a ticket
is compared when it is loaded, a missing log at once.

**Genesis pin and keys.** The genesis must be `seq` 1 (checked by the model, not only by the schema). `expected_genesis`
absent means the pin file in the host state directory, else trust on first use and pin. A new key is one above the highest
number in `keys.jsonl`, in any `ticket.created`, or among the first lines of the ticket logs, allocated under the lock.

**Idempotency.** A caller may pass a key. The store records the event id before the append and the result after it. A
retry returns the first result only if the log confirms it: same log, and the logged event is the requested one (type,
actor, payload; for a signed event its own id and base). A record that names anything else is ignored. A first attempt that
never reached the log gives its event id to the retry for an event nobody signs; a signed event keeps its signed id. The CLI
pins an attempt id per call across a crash (`Context.idem`).

**Revocations.** The host notes every person-key-signed device revocation it appends (with its time) in the host state
directory. On every load: a noted revocation the log lacks, that a later workspace `restore` post-dates, is re-appended
(host actor), which also finishes a re-append a crash interrupted; any other is a rolled-back revocation and the workspace
log is marked diverged until an owner `restore`. A `restore` is stamped no earlier than the revocations it must re-append.

**Lock.** One exclusive lock file per workspace, re-entrant within a process, `flock` (Windows: `msvcrt`); a call waits at
most ten seconds, then `store.busy` (retryable).

**Addon sections and artifacts.** Addon sections cannot be written until their manifest supplies a heading (C9). A file
artifact comes with its bytes, which must match the logged digest and size, and lands under `artifacts/`.

**Platforms.** macOS and Linux are supported. Windows is best effort and fails closed: files open in binary mode,
a replace is retried briefly on a sharing violation, and where `O_NOFOLLOW` is missing an opened file must be the file `lstat`
sees. Durability: the commit (the log append) uses `F_FULLFSYNC` on macOS (median 3.0 ms against 0.05 ms for a plain `fsync` on the
test laptop), `fsync` elsewhere.

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

- **Not in this file:** `status`, `people`, the claim, task state, acceptance-criteria state, answers, gate state,
  gate policy overrides and the artifact list. All of these come from events. The ticket document (§7) shows them
  derived.
- **Rebuildable.** Every value in this file comes from an event: `ticket.created`, the values in `ticket.updated`
  `set`, and the full question in `question.asked`. Replaying the ticket log rebuilds `ticket.json` byte for byte.
- **ids:** the host assigns `AC`, `T` and `Q` ids and never reuses or renumbers them.
- **Text rules:** §11.3.

**Every key is always present.** The store writes all 17 top-level keys in the order above, from the first event
on. Defaults for a new ticket:

| Key | Type | Default | Values |
|---|---|---|---|
| `title` | string, 1–200 Unicode scalar values, one line | (required at creation) | |
| `type` | token | (required at creation) | `feature`, `bug`, `chore`, `spike`, `epic` |
| `priority` | token | `medium` | `low`, `medium`, `high`, `urgent` |
| `size` | token or null | `null` (unsized) | `xs`, `s`, `m`, `l`, `xl` |
| `labels` | list of label tokens, unique | `[]` | `^[a-z0-9][a-z0-9._-]{0,31}$` |
| `parent` | key or null | `null` | |
| `blocked_by` | list of keys, unique | `[]` | |
| `due` | date or null | `null` | `YYYY-MM-DD` only |
| `visibility` | | `"workspace"` | `"workspace"` or `{"restricted": [person ids, at least one]}` |
| `links` | object, all four keys present | `{"repos": [], "branches": {}, "prs": [], "external": []}` | `repos`: repo names; `branches`: repo name → branch name; `prs`: `{repo, url}` with an `https://` url; `external`: `https://` urls |
| `acceptance` | list of `{id, text}` | `[]` | |
| `tasks` | list of `{id, text, verify, proves, assignee?}` | `[]` | `verify` is `null` or `{"cmd": str}`; `proves` lists AC ids that exist; `assignee` is optional (absent, never `null`) |
| `questions` | list of `{id, to, text, why?, options?, recommended?, blocking}` | `[]` | `to` is a person id or a ticket role (`ticket_owner`, `assignees`, `reviewers`, `watchers`); `options` items are `{key, label, cost?}`; `recommended` is one of the option keys |
| `addons` | object, `<addon name>` → object | `{}` | validated against the addon's manifest (§8) |

The `spike` type covers the format's "spike / investigation" column; there is no separate `investigation` type.
A repo name in `links` matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`; a branch name is a valid git ref name.

## 4. `body.md`

Prose only, with no frontmatter. A section starts with `## ` at column 0 outside a code fence, as in v1, including
v1's guards against forged headings. **Code fence (exact):** a line starting at column 0 with three or more backticks or three or more tildes opens a fence; it is closed by a line at column 0 of the same character, at least as long, with nothing after it but spaces. Inside a fence, `## ` lines are text. A `## ` line at column 0 outside a fence that isn't a known section heading is refused. Section text that ends with a fence still open is refused.

| Section (heading) | id | feature | bug | chore | spike | epic |
|---|---|---|---|---|---|---|
| Summary | `summary` | optional | optional | optional | optional | yes |
| Context (code, files, related tickets) | `context` | yes | yes | optional | yes | yes |
| Requirements | `requirements` | yes | yes | yes | yes (questions to answer) | yes |
| Out of scope | `out_of_scope` | yes | yes | — | — | yes |
| Plan | `plan` | yes | yes | yes | yes | — |
| Decisions (chosen, rejected, why) | `decisions` | yes | yes | optional | yes | yes |
| Verification | `verification` | yes | yes | — | — | — |
| Findings | `findings` | — | — | — | yes | — |
| Current state (handoff, at most about 10 lines, rewritten) | `current_state` | yes | yes | yes | yes | yes |

Limits: 65 536 bytes (UTF-8) per section and 262 144 bytes per `ticket.json`.

**Headings and section text.**

- The heading line is exactly `## ` plus the heading in the first column above (`## Out of scope`). The id is what
  events, hashes and `--section` use. Addon sections use the heading from their manifest and the id
  `<addon>.<section id>`.
- A heading that is not a section of this ticket's type (core or addon) is refused on write (`body.unknown_section`).
  A "—" section is refused the same way. Each section appears at most once.
- Nothing but empty lines may come before the first heading.
- **Section text** is everything between its heading line and the next heading line (or the end of the file), with
  leading and trailing LF characters removed. Nothing else is trimmed: spaces, tabs and blank lines inside the text
  are kept and hashed.
- The store writes `## <heading>\n\n<text>\n\n` per section, in table order, addon sections after the section their
  manifest names, and a single final LF. An empty section is written as `## <heading>\n\n`.

**Drafts and completeness.** A body may be incomplete at any time: a "yes" section may be missing or empty while a
ticket is being written. Completeness is enforced at the transitions, not on every write:

| Transition | Must be non-empty (where the type has them) |
|---|---|
| `requirements` approval | `summary` (epic), `context`, `requirements`, `out_of_scope`; at least one acceptance criterion |
| `plan` approval | `plan`, `decisions`; at least one task |
| `submit` | `verification` (or `findings` for a spike); every acceptance criterion has evidence (§6) |

An approval of an incomplete gate is refused with `gate.incomplete` and the list of what is missing. The JSON
Schema for `body` therefore requires no section; the model checks completeness.

## 5. Events

There are two logs with the same format:

- the **ticket log** `tickets/<uid>/events.jsonl`;
- the **workspace log** `events/workspace.jsonl` (workspace, members, devices, roles, policies, settings, grants,
  addons).

Each line is `cj(event)` (§11.2) followed by one LF. A line whose bytes are not exactly `cj` of its strict parse is
treated like a line with a bad `host_sig` (§5.5).

### 5.1 The envelope

| Field | Type | Rule |
|---|---|---|
| `v` | int | the envelope version, always `2` |
| `id` | ULID | chosen by whoever builds the event (the signer for person events). A repeated `id` in the same log is refused. |
| `seq` | int | `1` for the first event of a log, then +1, no gaps |
| `at` | timestamp | the host's clock when it appends |
| `type` | string | a type from §5.4; from P2 also `<addon>.<verb>` |
| `actor` | object | always present (§5.2) |
| `based_on` | hash or null | the head of an existing event in the same log with a lower `seq`: what the actor saw. `null` only on `seq` 1. Anything else is refused (`event.bad_base`). Staleness is decided by `gate_gen`, `hash` and `base_rev`, not by `based_on`. |
| `prev` | hash or null | the log head when the host appended. `null` only on `seq` 1. |
| `hash_v` | int | always `1` in this version (the recipe of §5.5 and §5.6) |
| `ws_seq` | int ≥ 1 | ticket-log events only: the workspace-log `seq` when the host appended (§5.5) |
| `roster_v` | int ≥ 0 | person events only: the member-list version at the event's position (§5.4.2). Named `roster_v` so it isn't confused with protocol §7.2's `list_seq`, which counts the WSK-signed device list. |
| `auth` | token | person events only (§5.3) |
| `sig` | b64u | person events only (§5.3) |
| `host_sig` | b64u | every event (§5.5) |

Then the payload fields of the type (§5.4). Each type has an exact field set: a missing required field or any extra
field is refused. Payload fields never reuse an envelope name.

```json
{"v":2,"id":"01J9ZP…","seq":5,"at":"2026-10-09T09:10:11Z","type":"gate.approved","gate":"requirements","hash":"sha256:fa37…","policy_hash":"sha256:31c2…","gate_gen":2,"hash_v":1,"ws_seq":31,"roster_v":7,"actor":{"kind":"person","id":"p_sev","device":"d_mac"},"auth":"passphrase","based_on":"sha256:aa91…","prev":"sha256:aa91…","sig":"Mz4…","host_sig":"q8T…"}
{"v":2,"id":"01J9ZQ…","seq":9,"at":"2026-10-09T10:40:22Z","type":"task.done","task":"T2","receipt":{"cmd":"dbt seed --select tariffs","exit":0,"ms":38200,"repo":"acme-energy-dbt","commit":"b7e1f02c…"},"actor":{"kind":"agent","id":"claude-code","session":"s_01J9…","for":"p_sev","grant":"gr_01J9…"},"based_on":"sha256:51e0…","prev":"sha256:51e0…","hash_v":1,"ws_seq":31,"host_sig":"Zr1…"}
{"v":2,"id":"01J9ZR…","seq":10,"at":"2026-10-09T10:41:03Z","type":"artifact.added","name":"seeds-in-warehouse.png","kind":"screenshot","sha256":"sha256:3f9a…","bytes":84213,"ac":"AC1","actor":{"kind":"agent","id":"claude-code","session":"s_01J9…","for":"p_sev","grant":"gr_01J9…"},"based_on":"sha256:98ac…","prev":"sha256:98ac…","hash_v":1,"ws_seq":31,"host_sig":"b2W…"}
```

### 5.2 Actors

| Kind | Shape | Signs | May append |
|---|---|---|---|
| person | `{kind: "person", id, device}` | always: `sig` and `auth` | types marked P or A in §5.4 |
| agent | `{kind: "agent", id, session, for, grant}` | never | types marked A |
| agent, unattended | `{kind: "agent", id, session, unattended: true}` | never | only `question.asked` (new ids only), `log.added` and `artifact.added` (no `ac`, no `task`, not `feedback`), on tickets with `visibility: workspace`, within the quotas below (A3, §12 N9) |
| addon (from P2; refused in P1) | `{kind: "addon", id: <addon name>, grant: <id of its addon.granted event>}` | never | its own `<addon>.*` events, and `ticket.updated` of leaf paths `ticket.addons.<its name>.<field>` whose `set_by` includes `addon` (§12 N11) |
| host | `{kind: "host"}` | never (only `host_sig`) | types marked H, and nothing else |

- **Every event has an actor.** Host events (`edit.external`, `projection.repaired`, `gate.invalidated`, …) carry
  `{"kind": "host"}`. "Unattributed" (T9) means no person or agent is credited; it does not mean the field is
  missing. There is one envelope schema, not two.
- **A person event is always signed**, whatever its type (§12 N1). An agent can use the workspace key in P1 (core
  §4), so it could forge any unsigned event; an unsigned person actor would let it put words into a person's mouth.
- `sig`, `auth` and `roster_v` appear on person events and on no others.
- Types marked H are refused from every other actor, and the host never appends a P or A type in its own name.
- An agent's `for` must be the person who issued `grant`. The grant must be valid at the event's position: not
  expired against `at`, not revoked, its scope covers the ticket and its verbs cover the operation (§10.1 A3).
  `unattended` and `for`/`grant` never appear together.
- **Unattended quotas:** per session, at most 30 unattended events and 20 MiB of artifacts per hour; per workspace,
  at most 120 unattended events and 100 MiB per rolling hour, measured over `at` in `(now − 3600 s, now]`. More is
  refused with `quota.unattended`. An unattended `artifact.added` changes the verify gate's input, so it can void a
  pending verify approval; this is accepted and visible in the log. An unattended artifact is never evidence (§6).
- **No addon actor in P1.** Addons don't run in P1 (A5), so an event with an addon actor is refused.
- **Addon events never carry core authority.** `<addon>.*` events change no core state (status, people, gates,
  claims, evidence, grants); only the addon's own view reads them.
- **Claims across persons.** A takeover (`claim.taken` with `takeover`) works for any agent whose grant covers the
  ticket, also one acting for another person; it is logged with its reason. A person (ticket owner, owner or
  maintainer) may release anyone's claim with a signed `claim.released` (`released`), naming the session of a live
  claim; anything else is refused (`claim.not_live`). Both are accepted for P1.

### 5.3 Human signatures, devices and `auth`

- **Key.** (One exception to "decrypted for one signature": `orch import v1`, §14.2.2. After the person has read the import review and typed `IMPORT <n>`, one passphrase unlocks `dk_sig` for that command only. The unlocked key signs only the person events of the reviewed plan, is held in that process with core dumps and debugger attach disabled, and is zeroised when the command ends. No other command, agent or process can use it.) A person event is signed with the **device signing key** (`dk_sig`) of `actor.device`. The device's
  certificate (orch-relay protocol §6.1, signed by the person key) is in the workspace log. The person key signs
  only device certificates, revocations and the workspace delegation (protocol §6, §7.1).
- **Custody of `dk_sig`** (§12 O2). `dk_sig` is held by the custody backend named in `auth` (D64). In P1
  (`passphrase`) the device signing key is encrypted under the passphrase exactly like the person key (D65), is
  decrypted only for one signature on a TTY, and is never cached. A device key that can sign without the factor its
  `auth` names is a `file`-tier key and never signs person events.
- **Device scopes.** A person event counts only if the signing device's certificate is valid at the event's
  position: not expired against `at`, not removed or revoked, and its `scopes_max` is a level list (a prefix of
  `look, decide, operate, type`, protocol §6.1) that contains `decide`. Certificates with a `drop:` scope
  (protocol §6.4) are refused in `device.added` and never sign person events. The member-management, policy, settings, grant, addon, `restore` and `invalid.acknowledged` events also need
  `operate`.
- **Adding a device.** `device.added` is signed by an **existing** valid device of the same person, so a new key is
  never self-authorising. A person's first device comes in with the event that introduces their person key:
  `workspace.created` for the owner, `member.added` for everyone else (`device_cert`). **Recovery (D50, §12 R3):**
  when the person has no valid device left, `device.added` may be signed by the new device itself; its certificate
  is signed by the person key and has `decide`, so the person key still vouches for it.
- **Revoking a device.** The authority of `device.revoked` is the embedded revocation (protocol §6.2), verified
  under the person's `pk_pub` for a device certificate of that person, not the sender. Any member's device may
  append it (actor P), and so may the host (actor H), for example after a `restore` or when a person has no device
  left.
- **Signed bytes (signed-event contract 1):** `"orch/v2/sig/ticket-event|" || cj({"contract": 1, "suite": 2,
  "workspace_id": W, "log": <uid>, "event": E})` for the ticket log, and `"orch/v2/sig/ws-event|" ||
  cj({"contract": 1, "suite": 2, "workspace_id": W, "log": "workspace", "event": E})` for the workspace log.
  `suite` is the deployment suite (2, D46). `E` is the event without `seq`, `at`, `prev`, `ws_seq`, `sig` and
  `host_sig`. The signature covers `id`, `type`, `actor`, `auth`, `based_on`, `roster_v`, `hash_v`, `gate_gen` (on
  decisions) and the payload. It can't be replayed into another ticket, log or workspace, and the generation (§5.7)
  stops a delayed decision from landing after a later one. A change to these bytes is a new `contract`, never a
  silent edit.
- **`auth` is custody metadata, not proof** (§12 O1). It names the backend the signer's client used (D64, D66):
  `passphrase`, `secure-enclave` (Mac, user presence per signature, D41), `secure-enclave-unlocked` (iPhone, D49),
  `webauthn`, `tpm`, `windows-hello`. It is covered by the signature, so nobody else can change it, but the format
  can't prove the factor: the device certificate (protocol §6.1) has no backend field, and F1 adds none. The factor
  is recorded in the event's `auth`, not in the certificate; D64's wording ("recorded in the person's device
  certificate") needs amending. A workspace policy may later refuse named factors for named verbs (D66); the
  default accepts any. The weaker guarantees stay as stated where they were decided: `secure-enclave-unlocked`
  signs whenever the phone is unlocked (D49), and `passphrase` does not protect a passphrase typed into a terminal
  an agent can read (D65, orch-v2-portable-custody.md §3).
- `presence` (in the 8 Oct draft) is replaced by `auth`.
- **Phone decisions (P3/P4).** A phone answers a question with protocol §13's `decision`, signed over
  `"orch/v2/sig/decision|" || cj({workspace_id, question_id, content_hash, decision_id, answer})`. **These signed
  bytes differ from `sig/ticket-event|`**, and one can't be turned into the other. P3 defines how the host records
  a verified decision (a new envelope `contract`, with the decision kept verbatim as evidence). **In P1 a
  `question.answered` with `evidence` is refused**; every person event carries `sig`, `auth` and `roster_v`.

### 5.4 Event types

Actor column: **P** person only (signed); **A** agent with a grant, or a person (signed); **U** also an unattended
agent; **D** an addon; **H** the host only. Types marked D58–D60 are new with those decisions. Field types: §11.1.
`?` marks an optional field (absent, never `null`, unless the type says "or null"). "Sections" in an edit event is
`{section id: {"hash": section hash, "refs": [artifact names, sorted, unique]}}`, with `null` for a removed section
(§5.8).

#### 5.4.1 Ticket log

| Type | Actor | Payload | Notes |
|---|---|---|---|
| `ticket.created` | A | `key`: key; `ticket_type`: ticket type; `title`: str; `owner`: person id | Always `seq` 1. `owner` is the person actor, or the agent's `for`. (`ticket_type`, because `type` is the envelope's.) |
| `ticket.updated` | A, D (from P2) | `base_rev`: {path: hash}; `set?`: {path: value}; `sections?`: sections | At least one of `set`, `sections`. Leaf paths only (§5.8). Refused on `done` and `closed` tickets for bound paths, from every actor (§5.7). |
| `status.changed` | A | `from`: status; `to`: `backlog` or `open`; `reason?`: str | Manual moves only (§5.9). |
| `ticket.submitted` | A | (none) | §5.9. |
| `ticket.closed` | P | `resolution`: `wont_do`, `duplicate`, `obsolete` or `other`; `duplicate_of?`: key; `text?`: str | `duplicate_of` only with `duplicate`. |
| `ticket.reopened` | P | `text?`: str | Raises every gate's generation. |
| `visibility.changed` | P | `visibility`: as in `ticket.json` | Also rewrites `ticket.json`. |
| `people.changed` | P | `role`: `owner`, `assignees`, `reviewers` or `watchers`; `add`: [person id]; `remove`: [person id] | For `owner`, `add` has exactly one id and the old owner is removed. On `done`/`closed` refused for roles a gate policy names. |
| `policy.changed` | P | `gates`: {gate: policy} | In the ticket log: an override, intersected with the workspace policy (§5.7). |
| `claim.taken` | A | `takeover?`: {`from_session`: session id, `reason`: str} | Agents only. One claim per ticket. Refused on `done`/`closed`. |
| `claim.released` | A, H | `session`: session id; `reason`: claim release reason (§11.4) | An agent releases only its own claim; a person (§5.2) only a live claim, named by its `session`. |
| `task.started` | A | `task`: task id | The lease (A4). Agents only. |
| `task.done` | A | `task`; `receipt?`: {`cmd`: str, `exit`: int, `ms`: int, `repo`: repo name or null, `commit`: git commit id or null}; `log?`: artifact name; `text?`: str | With `--run`, `exit` must be 0, otherwise nothing is appended. `receipt.cmd` must equal the task's `verify.cmd` at append. `commit` is the head of `repo`, read by the CLI from git. |
| `task.skipped` | A | `task`; `reason`: str | |
| `task.blocked` | A | `task`; `reason`: str | |
| `task.reopened` | A | `task`; `reason?`: str | |
| `handoff.written` | A | `text`: str, at most 2 048 bytes | Rewrites Current state. `orch handoff` also releases the claim (`claim.released`, `handoff`). |
| `log.added` | A, U | `text`: str, at most 4 096 bytes | Agent notes and human comments. |
| `question.asked` | A, U | `question`: the full question object as in `ticket.json` (`id`, `to`, `text`, `why?`, `options?`, `recommended?`, `blocking`); `qid`: 32 hex; `hash`: question hash | The last `question.asked` for an id defines it; `ticket.json` `questions` is its projection. `qid` is derived (§5.6). Re-asking an existing id needs a grant or a person; unattended only creates new ids. |
| `question.answered` | P | `question`: question id; `hash`; `option?`: option key; `text?`: str | At least one of `option`, `text`. Only the `to` person or a holder of the `to` role, or an owner or maintainer; when `to` resolves to no current member, owners and maintainers. |
| `gate.approved` | P | `gate`: `requirements`, `plan` or `code`; `gate_gen`: int; `hash`: gate hash; `policy_hash`; `source_sha?`: source list | `source_sha` only and always on `code` (D59). |
| `gate.changes_requested` | P | `gate`: `requirements`, `plan` or `code`; `gate_gen`; `hash`; `policy_hash`; `text`: str | |
| `verdict.given` | P | `outcome`: `pass` or `fail`; `gate_gen`; `hash`: verify gate hash; `policy_hash`; `source_sha`: source list; `text?`: str | The verify gate's decision. `text` is required on `fail`. `source_sha` is D58. |
| `gate.invalidated` | H | `gate`: gate; `cause`: `new_commits`, `content_changed`, `policy_changed`, `member_changed`, `device_compromised` or `conflict_resolved`; `voided`: [event id] | **D58.** A record only: it raises no generation and changes no status. `voided` is derived (§5.11). `conflict_resolved` is D53 (P2). |
| `branch.pushed` | H | `repo_name`: repo name; `repo_id`: repo identity; `ref`: str; `sha`: git commit id; `before`: {`repo_id`, `ref`, `sha`} or null | **D58.** Appended whenever any of (`repo_id`, `ref`, `sha`) of a linked repo differs from the source-list projection, including the first observation (`before: null`). Never taken from an agent. |
| `artifact.added` | A, U | file: `name`, `kind`, `sha256`: hash, `bytes`: int; or addon: `name`, `kind`, `addon`, `ref`: str; both: `task?`, `ac?`, `label?`: str | `kind` `feedback` only from a person. Unattended: no `ac`, `task`. Refused on `done`/`closed`. |
| `artifact.replaced` | A | as `artifact.added`, plus `replaces`: hash | Same `name`; `replaces` is the old digest. |
| `edit.external` | H | `sections`: sections; `voided_gates`: [gate]; `normalised`: bool | A `body.md` change the host didn't write (§5.8). `voided_gates` and `normalised` are derived. Not followed by `gate.invalidated`. |
| `projection.repaired` | H | `path`: str; `cause`: `external_edit`, `projection_mismatch` or `keys_mismatch`; `fields?`: [path] | Both logs. |
| `restore` | P | `from_seq`: int; `head`: hash; `abandoned`: {`seq`: int, `head`: hash} or null; `abandoned_decisions`: [event id]; `reason`: str | Both logs. Owner only (§5.10). |
| `invalid.acknowledged` | P | `invalid_seq`: int; `invalid_head`: hash; `reason?`: str | Both logs. Owner only. Names an event that failed authorization (§5.11); lifts the decision freeze, keeps the event absent. |

#### 5.4.2 Workspace log

| Type | Actor | Payload | Notes |
|---|---|---|---|
| `workspace.created` | P | `workspace_id`; `prefix`; `host_id`; `wsk_pub`: b64u; `owner`: {`person`: person id, `name`: str, `pk_pub`: b64u}; `delegation`: signed object; `device_cert`: signed object | Always `seq` 1, `roster_v: 0`: the **genesis** (§5.11). Makes `owner.person` a member with role `owner`; the member-list version after it is 1. |
| `member.added` | P | `person`: person id; `name`: str; `role`: member role; `pk_pub`: b64u; `device_cert`: signed object | `person` = `"p_" + person_id(pk_pub)`; `device_cert` is their first device, signed by `pk_pub`. |
| `member.removed` | P | `person` | Releases claims (`member_removed`). Effects in §5.7. |
| `role.changed` | P | `person`; `role`: member role | |
| `device.added` | P | `device`: device id; `cert`: signed object | Protocol §6.1 certificate, verbatim. Signed by an existing device of the same person, or by the new device when none is left (§5.3). |
| `device.removed` | P | `device`; `reason?`: str | Removes the device from **this** workspace only (protocol §6.3). By the device's person or an owner. |
| `device.revoked` | P, H | `device`; `reason`: `compromised`, `lost` or `retired`; `revocation`: signed object | The global revocation (protocol §6.2), signed by the person key, verbatim; its authority (§5.3). `reason` must equal `revocation.o.reason`, otherwise refused. Every workspace refuses the device from then on. `compromised` has the effects in §5.7. |
| `policy.changed` | P | `gates`: {gate: policy} | The workspace defaults. |
| `settings.changed` | P | `set`: {`grant_hours?`: int 1–24, `claim_ttl_min?`: int 15–1440, `lease_ttl_min?`: int 5–1440, `repos?`: {repo name: {`path`: str} or null}} | **D60** (`grant_hours`). Existing grants keep their end time. `null` removes a repo. Refused when two repos resolve to the same path. |
| `grant.issued` | P | `grant`: grant id; `scope`: `all` or `workable`; `verbs`: `"agent"` or [operation name]; `issued_at`: timestamp; `hours`: int; `expires_at`: timestamp; `secret_hash`: hash; `label?`: str | **D60** terms in §10.1 A3. Always for the signer. Readers check `\|at − issued_at\| ≤ 300 s`, `expires_at == issued_at + 3600·hours` (seconds, no leap seconds) and the role terms at the event's position. |
| `grant.revoked` | P | `grant`; `reason?`: str | **D60.** |
| `addon.granted` | P | `name`: addon name; `version`: str; `package_sha256`: hash; `capabilities`: [token]; `binds`: {`fields`: {field: [gate]}, `sections`: [{`id`, `gate`: [gate], `types`: [ticket type]}]} | Owner only. Also enables the addon. The host checks `binds` against the package's manifest; replay uses only `binds`. |
| `addon.disabled` | P | `name` | Data stays untouched. A new `addon.granted` enables it again. |
| `addon.purged` | P | `name` | Deletes the addon's data. |

The **member-list version** is the number of `workspace.created`, `member.added`, `member.removed` and
`role.changed` events in the workspace log so far (so 1 right after the genesis). **Authorization is evaluated at
the event's position in the merged order (§5.5).** A person event's `roster_v` must equal the member-list version at
that position, or it is refused (`members.stale`) and the signer re-signs. An old version is never a licence to act
with an old role.

**Who may sign what** (on top of the actor column and the device scopes of §5.3):

| Events | Who |
|---|---|
| `gate.*`, `verdict.given` | eligible approvers of that gate (§5.7) |
| `ticket.closed`, `ticket.reopened`, `people.changed`, `visibility.changed`, ticket `policy.changed` | the ticket owner, workspace owners and maintainers |
| `member.added`, `member.removed` | owners; maintainers for the roles member and viewer |
| `role.changed`, workspace `policy.changed`, `settings.changed`, `addon.*`, `restore`, `invalid.acknowledged`, `workspace.created` | owners |
| `device.added` | the device's own person, from an existing device, or the new device when none is left |
| `device.revoked` | any member's device, or the host; the embedded revocation must be signed by the device's person key |
| `device.removed` | the device's own person; owners for any device |
| `grant.issued`, `grant.revoked` | §10.1 A3 (D60) |

**Unknown types.** The prefixes `ticket`, `status`, `visibility`, `people`, `policy`, `claim`, `task`, `handoff`,
`log`, `question`, `gate`, `verdict`, `branch`, `artifact`, `edit`, `projection`, `restore`, `workspace`, `member`,
`role`, `device`, `settings`, `grant`, `addon` and `invalid` are reserved: no addon may take one of these names, and an
unknown type under one of them is refused. Custom `<addon>.<verb>` events are deferred (A5); in P1 they are
refused.

**Not events.** Refusals (`human_only`, stop rule) are kept in the session records in `.state/`, not in a log.
Workspace views, agent starts, relay links, epochs and terminal events are defined with their phases (P2, P3).

### 5.5 Signing by the host, order, the chain and writes

- **`host_sig`** = `Sign(WSK, "orch/v2/sig/host-event|" || cj({"contract": 1, "suite": 2, "workspace_id": W,
  "log": L, "event": E}))`, where `L` is the uid or `"workspace"`, and `E` is the full event without `host_sig` (so
  it includes `seq`, `at`, `prev`, `ws_seq` and `sig`).
- **Event head:** `head(e) = "sha256:" + hex(SHA-256("orch/v2/event|" || cj(e)))` over the full event, `host_sig`
  included, computed from the strictly parsed object, never from the raw line.
- **Chain:** `prev` of event `n` is `head` of event `n−1`; `prev` of `seq` 1 is `null`. The **log head** is the head
  of the last event. An empty log has no head.
- **Merged order.** `ws_seq` is non-decreasing along a ticket log and at most the workspace log's last `seq`;
  otherwise the line fails like a bad `host_sig`. The merged order of all logs is by `ws_seq`, the workspace log
  first, then ticket events ordered by `(at, uid, seq)` (§5.11): an event with `ws_seq = k` is evaluated against the
  workspace state after workspace event `k`. The host holds the workspace-log lock (shared) while it appends a ticket event. `ws_seq` and
  `at` are chosen by the host and not signed by the person (§12 N4).
- **Reading** a log: strictly parse each line, check that it is `cj`, check the field set, `seq`, `prev`, `ws_seq`
  and `host_sig`, then `sig` against the device certificate, then replay authorization (§5.11). A line that fails
  these checks breaks the chain: every read and `orch doctor` report `chain.broken` with its `seq`, and nothing
  after it counts until an owner-signed `restore`. An event that passes them but fails authorization is handled as
  §5.11 says.
- **Writing** is atomic per event, under the store lock: (1) write the new `ticket.json`/`body.md` to
  `.state/pending/<event id>/`; (2) append the event line and fsync; (3) rename the pending files into place, fsync
  the directory, keep a copy of the installed `body.md` in `.state/body/<uid>.md`, and record the event id in
  `.state/applied`. **Crash recovery** runs at the next lock, only when the last event's id differs from
  `.state/applied`: if its pending files exist and match its hashes, finish step 3; if they don't, report
  `store.torn_write`, rebuild `ticket.json` from events and append `edit.external` for the `body.md` on disk.
  The copy in `.state/body/` is used for a revert or a recovery only when its section hashes match the log;
  otherwise the host reports `store.torn_write` and doesn't use it.
  Pending files without their event are deleted. A file change without an event is never treated as the host's
  write; when `.state/applied` matches, a mismatch is an ordinary external edit (§5.8).

### 5.6 Hashes

Every hash in the format is `sha256:` followed by 64 lower-case hex characters, artifact digests included. A hash
is parsed (one parser, which refuses anything else) and compared as bytes. A digest is only compared together with
the field it belongs to; no code looks a hash up by value alone.

| Hash | Definition (`H` = SHA-256) | Used in |
|---|---|---|
| artifact digest | `H(file bytes)` | `artifact.*`, gate `artifacts`, `addon.granted` `package_sha256` |
| section hash | `H("orch/v2/section\|" \|\| UTF-8(section text))`; a missing section has the hash of `""` | edit events, `base_rev`, gate `sections` |
| value hash | `H("orch/v2/value\|" \|\| cj(value))` | `base_rev` for `ticket.json` paths |
| gate hash | `H("orch/v2/gate\|" \|\| cj(G))`, `G` in §5.7 | `gate.*`, `verdict.given` |
| policy hash | `H("orch/v2/policy\|" \|\| cj({"gate": gate, "policy": P}))`, `P` the effective policy in canonical form (§5.7) | `gate.*`, `verdict.given`, gate hash |
| people hash | `H("orch/v2/people\|" \|\| cj({role: value}))` over only the ticket roles the gate's effective policy names in `approvers` or `not`, plus `assignees` when `independent` is on; lists sorted, `owner` as `ticket_owner`, a person id or null | gate hash |
| question id (`qid`) | the first 16 bytes, in 32 hex, of `H("orch/v2/question-id\|" \|\| cj({"workspace_id": W, "ticket": uid, "question": "Q1"}))` | `question.asked`; the protocol §13 `question_id` |
| question hash | `H("orch/v2/question\|" \|\| cj({"question_id": qid, "ticket": uid, "text", "options"}))` (protocol §13's shape; `options` `[]` when none) | `question.*` |
| event head | `H("orch/v2/event\|" \|\| cj(event))` | `prev`, `based_on`, checkpoints |
| genesis | the event head of `workspace.created` | the trust root (§5.11) |
| grant secret hash | `H("orch/v2/grant-secret\|" \|\| secret bytes)` | `grant.issued` |

- The artifact digest is the one unlabelled hash: it must match `sha256sum` of the file. It is never compared with
  any other kind of hash.
- **Refuse, don't normalise.** Every hash function refuses an input string that breaks the text rules (§11.3)
  instead of normalising it. Normalising happens once, when text enters the store.
- New labels (`orch/v2/gate|`, `section|`, `value|`, `policy|`, `people|`, `question-id|`, `event|`,
  `grant-secret|`, `sig/ticket-event|`, `sig/ws-event|`, `sig/host-event|`, `sig/checkpoint|`) go into the
  orch-relay `vectors_v2.json` label list; the table stays prefix-free.

### 5.7 Gates

**Gates.** The core has four, in this order: `requirements`, `plan`, `verify`, `code`. Addons can't add gates;
their fields and sections join one of these (`binds`). In `binds`, a section is always named in full as `<addon>.<token>`.

**Policy** (workspace default and ticket override). Every policy object, workspace or override, has all five keys:

| Key | Type | Rule |
|---|---|---|
| `approvers` | list of approver tokens, at least one | who may approve |
| `count` | int ≥ 1 | how many distinct persons must approve at the current hash and generation |
| `not` | list of approver tokens, may be empty | excluded, even when also in `approvers` |
| `applies` | `"all"`, `"off"` or a list of ticket types | the workspace default is `"all"`, for `code` `"off"` (D59) |
| `independent` | bool | default `false`; `true` adds the independence rule below. Always `true` for `code`. |

- **Approver tokens:** the workspace roles `owner`, `maintainer`, `member`, and the ticket roles `ticket_owner`,
  `assignees`, `reviewers`, `watchers`. `owner` always means the workspace role; the ticket's owner is
  `ticket_owner`. A viewer never approves.
- **The effective policy is an intersection,** recomputed whenever either side changes: `approvers` = workspace ∩
  override, `count` = the larger, `not` = the union, `independent` = either. `applies` is the union: `"all"` if
  either side is `"all"`; `"off"` only if both are `"off"`; otherwise the sorted, de-duplicated list (kept as a list
  even when it names every type). An override can never end up looser, even after a later workspace change.
  "No eligible approver" is judged on tokens, not persons: an override that leaves no token is refused; if a later
  workspace change empties the set, the gate is blocked (`gate.no_eligible`) until someone fixes the policy.
- **Canonical form** for hashing: all five keys, `approvers` and `not` sorted and de-duplicated, `applies` a non-empty list. Policies and people lists are stored in events and files **in this canonical form** (people lists sorted and de-duplicated too); a non-canonical one is refused at append. Hashing applies the canonical form as well, as a safeguard; every other list this document calls "sorted" (for example `source_sha`, `prior.approvals`) must already be sorted and is refused otherwise. "Sorted" always means by Unicode code point (equal to UTF-8 byte order).
- For `code`, `not` always includes `assignees` and `independent` is `true`; the host refuses a policy without them
  (D59).

**Bound paths.** `bound(g)` is the set of paths whose edits gate `g` depends on:

| Gate | `bound(g)` |
|---|---|
| `requirements` | `ticket.type`, `ticket.size`, `ticket.acceptance`, `body.summary`, `body.context`, `body.requirements`, `body.out_of_scope`, addon paths per `binds` |
| `plan` | all of `requirements` plus `ticket.tasks`, `body.plan`, `body.decisions`, addon paths per `binds` |
| `verify` | `ticket.type`, `ticket.size`, `ticket.acceptance`, `ticket.links`, `body.verification`, `body.findings`, addon paths per `binds` |
| `code` | `ticket.type`, `ticket.size`, `ticket.acceptance`, `ticket.links` |

Not bound by any gate, so never presented as approved: `title`, `priority`, `labels`, `parent`, `blocked_by`, `due`,
`visibility`, questions (also `why` and `recommended`), Current state, and addon fields no `binds` names.

**Generations.** Each gate `g` of a ticket has a generation, starting at 0. The events in this table raise it **in
every status**. **An event raises a gate by at most 1, however many rows match** (when `g` applies):

| Event | Raises |
|---|---|
| `ticket.updated` with a path in `bound(g)`, whether or not the value changed | `g` |
| `edit.external` naming a section of `g` | `g` |
| `artifact.added`, `artifact.replaced` | `verify`; `requirements`/`plan` only when the name is in the `refs` of one of their sections |
| `task.done`, `task.reopened`, `task.skipped` | `verify` |
| `branch.pushed` | `verify`, `code` |
| `people.changed` for a role named in `g`'s effective policy (or `assignees` when `independent`) | `g` |
| `policy.changed` (ticket or workspace) whose `gates` names `g` | `g` |
| `addon.granted`, `addon.disabled`, `addon.purged` whose old or new `binds` names `g` | `g` |
| `member.removed`, `role.changed`, `device.revoked` (`compromised`) that voids a counting decision of `g` | `g` |
| `gate.changes_requested` on `g` or an earlier gate; `verdict.given` `fail` | `g` and every later gate |
| `ticket.reopened`, `restore` | every gate |
| a raise of an earlier gate, or a change of its first-`count` counting approvals | `g` |

`gate.invalidated` raises nothing. Edits of bound content are refused on `done` and `closed` tickets, so there the
only raises are people and policy changes, `ticket.reopened`, `restore` and `branch.pushed`. "Done is sticky" is a
separate status rule (below): it decides when a ticket leaves `done`, not whether a generation goes up. After a
reopen, the old verdict and code approvals therefore no longer count. The generation is derived by replaying both logs in the merged order, so it
needs only events. A decision (`gate.approved`, `gate.changes_requested`, `verdict.given`) carries the `gate_gen`
its signer saw, inside the signed bytes. **A decision whose `gate_gen` is not current is refused at append and never
counts on replay.** So a delayed approval can't land after a later rejection, and **a voided approval is retired for
good**: reverting the content brings back the old hash, but not the old generation.

**Gate hash input `G`.** These 15 keys are always present, for every gate (empty values where a gate doesn't use
one). Every value comes from the logs, so any reader can rebuild `G`:

| Key | Value |
|---|---|
| `workspace_id` | 32 hex |
| `uid` | the ticket uid |
| `gate` | the gate name |
| `schema` | `"orch.ticket/2"` |
| `hash_v` | `1` |
| `sections` | {section id: section hash} for each section of the gate (table below) that this ticket's type has; the hash of `""` when it is missing. The prompt shows the text and checks it against the hash. |
| `fields` | `{"ticket_type", "size", "acceptance": [{id, text}], "links", "addons": {addon: {field: value}}}`; `links` is `ticket.links` for `verify` and `code`, `null` otherwise; `addons` holds the fields whose `binds` names this gate, a missing value as `null`; `{}` when none |
| `addon_packages` | {addon: `package_sha256`} for every addon that is granted, not disabled and not purged, and whose `binds` names this gate for a field, or for a section of this ticket's type; `{}` when none |
| `tasks` | plan gate: `[{id, text, verify, proves}]` in `ticket.json` order; every other gate: `[]` |
| `artifacts` | {name: {`kind`, `digest`, `ac`, `task`}} (`ac`, `task` `null` when absent): for `requirements` and `plan` the file artifacts named in the `refs` of the gate's sections; for `verify` every file artifact in the manifest; for `code` `{}` |
| `receipts` | `verify`: {task id: {`event`, `repo`, `commit`, `exit`}} from the latest `task.done` of each task that is currently done and has a receipt; every other gate: `{}` |
| `source_sha` | `verify` and `code`: the source list (below); every other gate: `[]` |
| `prior` | {earlier gate: {`gen`, `approvals`: [event ids]}} for each earlier gate that applies: its generation and the ids of the first `count` counting approvals by `seq`, sorted; `{}` for `requirements` |
| `policy_hash` | the policy hash of this gate |
| `people_hash` | the people hash of this gate |

| Gate | Sections |
|---|---|
| `requirements` | `summary`, `context`, `requirements`, `out_of_scope`, plus addon sections bound to it |
| `plan` | `plan`, `decisions`, plus addon sections |
| `verify` | `verification` (`findings` for a spike), plus addon sections |
| `code` | none (`{}`) |

The approval prompt recomputes the SHA-256 of every bound file and refuses on a mismatch (`artifact.mismatch`).
Addon artifacts (`addon` + `ref`) have no digest: they are not bound, the prompt marks them "not bound", and they
never count as evidence.

**The source list (D58, D59).** One entry `{"repo": repo identity, "ref": "refs/heads/<branch>", "sha": git commit
id}` per repo in `links.repos`, sorted by `repo`. Every repo a ticket names is code-bearing and must have a branch
in `links.branches` before `submit`. A ticket that links no repo has an empty source list, and its verify decisions
carry `source_sha: []`; a ticket that links a repo must have a non-empty list.
- **The current source list is the projection of the latest `branch.pushed` per repo name in `links.repos`.** The
  host appends `branch.pushed` whenever any of (`repo_id`, `ref`, `sha`) of a linked repo differs from that
  projection: the first observation (`before: null`), a new commit, a rebase, a force-push, a reset (also back to
  an older commit), a change of `links.repos` or `links.branches`, a change of `settings.repos`, and a change of the
  remote. A decision counts only if its `source_sha` equals the projection at its position.
- **A missing ref is not a change** (a branch deleted after its PR merged, a remote gone): nothing is appended. On a ticket that is not `done`, an approval prompt for `verify` or `code` is refused (`source.missing`) while a source ref is missing; it never shows the last known commit as current.
  **On a `done` ticket only a new `sha` on an existing ref counts**; an identity or ref change there is shown, not
  appended.
- The host appends any pending `branch.pushed` **before** it builds an approval prompt.
- Separately, the host reads git when it builds the prompt, **again when it appends the decision** (a mismatch is
  refused with `gate.stale`), and again at landing.
- **Repo identity.** From the raw `remote.origin.url`, without `insteadOf` rewriting: `https://` and
  `ssh://`/scp-like forms map to `https://host[:port]/path`, keeping every port except 443 (https) and 22 (ssh),
  userinfo (`user:token@`) always removed, host lower-case and nothing else changed, one trailing `.git` and `/`
  removed. Anything else (no remote, `file://`,
  a path) is `local:<repo name>`. The result must match the canonical form exactly, and anything that doesn't is
  **refused, never converted**: host labels `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?` joined by single dots (at most 253
  characters, no trailing dot; punycode `xn--` allowed; Unicode hosts and IPv6 refused; an all-numeric last label only
  as a plain dotted quad without leading zeros); port `[1-9][0-9]{0,4}` up to 65535, never 443; path segments
  `[A-Za-z0-9._~-]+` joined by single slashes, no empty, `.` or `..` segment, no trailing slash, no `.git` suffix in
  any case; ASCII only, no `%`, `?`, `#`, `@` or whitespace. A prompt is refused when two linked repos share an
  identity, compared **ignoring ASCII case** (hosts like GitHub treat `Acme/X` and `acme/x` as one repo); the hashed
  value keeps the raw form.
- **P1 limit:** the identity is read from a working copy the agent can write, so it protects against mistakes and
  aliasing, not against the agent.

**Who may approve.** An approval (`gate.approved` or a `pass` verdict) counts only if, at its position:
- the signer is a member with an eligible token, not excluded by `not`;
- it signed the current gate hash, the current `policy_hash`, the current `gate_gen` and (for `verify`, `code`) the
  current source list;
- the gate is complete (§4; an empty section is the hash of `""`), also on replay;
- when the policy is `independent`: the signer is not a **worker** of the ticket (§12 O4). A worker is anyone who
  was an assignee, held a claim, or was the `for` person of any agent event on the ticket (edits, `claim.taken`,
  `task.*`, `artifact.*`, and the commits a `branch.pushed` records for that agent's session) since the ticket was
  last reopened. The set only grows: a generation raise never clears it. The code gate always uses this rule
  (D59).

`independent` is off by default for `requirements`, `plan` and `verify`, so **a sole owner can approve the work of
their own agents**: with it off, a person may verify work their own agent did, even under `not: assignees`
(§12 O4). It is always on for `code`.

**Removal, role changes, revoked devices** (§12 O3). Eligibility is evaluated at the decision's position. A later
`member.removed` or `role.changed` voids that person's approvals only on gates that haven't yet reached `count`.
`people` lists are not changed by a removal: removed persons stay listed and become ineligible. A `device.revoked`
with reason `compromised` voids the decisions that device signed on tickets that are not yet `done`; a `done` ticket
it approved keeps its state and shows the flag "approved by a revoked device". It also ends every grant that device signed. `member.removed` ends every grant of the removed person and every
device of that person in this workspace: a later `member.added` starts with new devices and grants only.

A gate is approved when `count` distinct persons have counting approvals. A gate that doesn't apply to the ticket's
type is not needed.

**Done is sticky** (§12 O3). Once a ticket reaches `done`, its gates are frozen at the decisions that made it `done`.
It leaves `done` only by: `ticket.reopened`; `branch.pushed` for one of its source refs (D58); and the D59 code-gate
rule. While it is `done`, the done rule is not re-evaluated, whatever happens to its generations. On `done` and `closed` tickets the host refuses `ticket.updated` of bound paths, `artifact.*`, `task.*`,
`claim.taken` and `people.changed` for roles referenced by a gate policy, from every actor (persons too). An
external change to a bound `body.md` section is reverted like `ticket.json` (`projection.repaired`, cause
`external_edit`), from the host's copy in `.state/body/` when its section hashes match the log (otherwise
`store.torn_write`).

**Change requests.** `gate.changes_requested` on gate G, or a `fail` verdict, raises the generation of G and of every
later gate, so all their earlier approvals stop counting.

**Text in approved content.** The approval is refused (`gate.suspicious_text`) when gated text contains a bidi
control (U+202A–202E, U+2066–2069, U+200E, U+200F, U+061C). Other invisible characters (U+200B–200D, U+2060,
U+FEFF, tag characters, other `Cf`, and these non-`Cf` look-alikes: U+034F, U+115F, U+1160, U+2028, U+2029, U+3164, U+FFA0, U+FE00–U+FE0F, U+E0100–U+E01EF) are shown as `⟨U+200B⟩` in every approval prompt and in `orch show`
(§12 N5).

**The verdict binds to the commit (D58).** The verify gate hash includes the source list, and the prompt shows the
commits and the diffstat. Every change of a source ref is a `branch.pushed` (above); it raises the generation of
`verify` and `code`, and the host records voided approvals with `gate.invalidated` (`new_commits`). A `done` ticket
goes back to `testing`.

**Landing (D53) is not an exception to this.** D53's "a clean rebase keeps the approval" applies only to the
landing worker's own **candidate**: it records the approved `source_sha` and its derived `candidate_sha`
(`land.attempt`, orch-v2-land-skills.md §1), and the candidate is checked again before it merges. The ticket branch
itself is never rebased under an approval; if it is, D58 applies and the approval is void.

**The code gate (D59).** Off by default; turned on per workspace or per ticket type with `applies`, with an
optional `count`. Never approved by an assignee (`not` includes `assignees`, `independent` is `true`), never by a
charter or anything but a person's signature. It is approved after a `pass` verdict on the same source list (the
verdict's id is in its `prior`); landing needs both on that commit. Turning it on or raising `count` moves back
only `done` tickets with an open landing; landed or merged tickets stay `done` (the landing addon, P2, supplies
which is which; in P1 no ticket moves back).

### 5.8 Edits, `base_rev` and external edits

- **Paths.** `ticket.<key>` for a top-level key of `ticket.json` (`ticket.title`, `ticket.tasks`, …),
  `ticket.addons.<addon>.<field>` for an addon field, `body.<section id>` for a section. **Only leaf paths:**
  `ticket.addons` and `ticket.addons.<addon>` can't be set as a whole, so nobody replaces a parent object to get
  around a field's `set_by`.
- **`set_by` is checked per actor.** A write to an addon field is allowed when the actor matches one of the field's
  `set_by` alternatives: `agent` (an agent with a grant), `addon` (that addon only), or a human token (a signed
  person event by someone holding that role).
- **Protected paths** are never set by `ticket.updated`: `ticket.schema`, `ticket.uid`, `ticket.key` (never change),
  `ticket.visibility` (only `visibility.changed`) and `ticket.questions` (only `orch ask`, which appends
  `question.asked` and writes the question).
- **Inline artifact references.** A reference is every match of the regex
  `\(artifact:([A-Za-z0-9][A-Za-z0-9._-]{0,127})\)` in the section text, with no Markdown parsing (code fences
  included). A referenced name that isn't in the file-artifact manifest is refused on write (`body.unknown_artifact`).
  Edit events record each changed section as `{"hash", "refs"}`, so the references are in the log. The host
  refuses an edit whose `refs` differ from the regex over its text (`body.bad_refs`); this matters for
  person-signed edits, where the signer computes `refs`.
- **`base_rev`** maps each path the edit touches to the hash the editor last saw: the section hash for `body.*`, the
  value hash for `ticket.*`. The host refuses the edit with `conflict.section` (exit 8) when one of them is no longer
  current. Orch tracks it per session and path; agents never pass it.
- **External edits.** A `body.md` change the host didn't write becomes `edit.external` with the new section hashes
  and references (unknown references are kept and make the gate incomplete until fixed). It raises the generations
  per §5.7. If the file broke the text rules, the host rewrites it normalised and sets `normalised: true`. On a
  `done` or `closed` ticket a change to a bound section is reverted instead (§5.7). **Any** change to `ticket.json`
  the host didn't write is reverted, with `projection.repaired` naming the paths (§12 N6).

### 5.9 Status

`backlog`, `open`, `in_progress`, `testing`, `done`, `closed`. Status is derived from events. **Events not in this
table don't change status. A table event whose from-state isn't listed is refused** (for host events: the status
stays unchanged).

| Event | From | To |
|---|---|---|
| `ticket.created` | — | `open` |
| `status.changed` | `open`, `backlog` (no claim) | `backlog` or `open` |
| `claim.taken` | `open`, `backlog`; `in_progress` (takeover) | `in_progress` |
| `claim.released` | `in_progress` | `open`; other states unchanged |
| `ticket.submitted` | `in_progress` | `testing` (needs `requirements` and `plan` approved where they apply, and evidence for every AC) |
| `gate.approved` on `requirements` or `plan` | any but `done`, `closed` | unchanged |
| `verdict.given` `pass` that doesn't complete the done rule; `gate.approved` on `code` that doesn't | `testing` | unchanged |
| `verdict.given` `pass` that completes the done rule | `testing` | `done` |
| `gate.approved` (`code`) that completes the done rule | `testing` | `done` |
| `verdict.given` `fail`; `gate.changes_requested` on `code` | `testing` | `in_progress` |
| `gate.changes_requested` on `requirements` or `plan` | `open`, `backlog`, `in_progress`, `testing` | unchanged, except `testing` → `in_progress` |
| `branch.pushed` for a source ref | `done` | `testing`; other states unchanged |
| D59 code-gate rule (P2) | `done` with an open landing | `testing` |
| `ticket.closed` | any but `closed` | `closed` |
| `ticket.reopened` | `done`, `closed` | `open` |

Verdicts and `code` approvals and change requests are accepted only in `testing`. The **done rule**: `verify` has its count of `pass` verdicts, and `code` has its count where it applies, all at the
current generations. A duplicate approval by one person counts once. `gate.invalidated` changes no status. "Waiting" (a blocking question is open, or a gate waits
for a person) is derived and shown, not a status. A claim lapses when its session appended nothing for
`claim_ttl_min`, or when its grant ends; a task lease lapses after `lease_ttl_min`. The host appends
`claim.released` (`expired` or `grant_ended`) with the next write.

### 5.10 Checkpoints and restore

The host keeps WSK-signed checkpoints in `.state/checkpoints/` (P1) and publishes them to the relay and to member
devices from P3. A checkpoint is a protocol §2.4 signed object `{"o": …, "sig": …}`:

| Kind | `o` |
|---|---|
| `ticket_checkpoint` | `{"v": 2, "suite": 2, "kind": "ticket_checkpoint", "workspace_id", "uid", "seq", "head", "at"}` |
| `workspace_checkpoint` | `{"v": 2, "suite": 2, "kind": "workspace_checkpoint", "workspace_id", "genesis", "n", "at", "workspace_log": {"seq", "head"}, "tickets": {uid: {"seq", "head"}}}` |

- `sig = Sign(WSK, "orch/v2/sig/checkpoint|" || cj(o))`. `n` counts the workspace checkpoints from 1.
- Refused: a checkpoint with a lower `seq` (or `n`) than one already seen, and **one with the same `seq` but a
  different `head`** (`chain.diverged`). A log whose head at a checkpointed `seq` differs from the checkpoint is
  diverged too: reads report it and no new events are appended until a `restore`.
- **Restore** (§12 O5). After a rollback (a restored backup, a git force-push of the workspace repo) an owner signs
  `restore {from_seq, head, abandoned, abandoned_decisions, reason}`: `from_seq` and `head` name the last event on
  disk that stays valid; `abandoned` gives up every checkpoint above `from_seq` and names the highest (`null` if
  none); `abandoned_decisions` lists the ids of the signed decisions in the given-up part that the host still knows
  of. The host appends it as `from_seq + 1` with `prev = head` and accepts checkpoints of the new chain from then on.
  It raises every gate's generation, so no decision from the abandoned part counts again; a workspace-log `restore`
  does so on every ticket.
- **Restore never drops revocations.** The host keeps every PK-signed revocation it has seen in host state. A
  workspace `restore` is accepted only if the host re-appends each of them to the new chain at once, as
  `device.revoked` with actor H (allowed by the embedded revocation, §5.3).
- **What P1 can't detect:** if both the history and the local checkpoints in `.state/` are replaced, the rollback
  is invisible. From P3, checkpoints on the relay and on member devices catch it.

### 5.11 Trust root and authorization replay

- **Genesis.** The trust root of a workspace is the head of its `workspace.created` event. Readers check it in this
  order:
  1. `person_id(owner.pk_pub) == delegation.o.owner_person_id == device_cert.o.person_id`, and
     `owner.person == "p_" +` that id;
  2. the delegation's signature under `owner.pk_pub` (label `sig/ws-delegation|`) and its exact field set;
  3. `delegation.o.workspace_id == workspace_id` and `delegation.o.wsk_pub == wsk_pub`;
  4. this event's `host_sig` under `wsk_pub`;
  5. `device_cert` under `owner.pk_pub`, and `actor.device == "d_" + device_cert.o.device_id`;
  6. `sig` under `device_cert.o.dk_sig_pub`.
- **Pinning.** The host remembers the genesis outside the workspace (`<host state dir>/hosts/<workspace_id>/genesis`)
  and puts it in every workspace checkpoint. The person's client records it in its custody key file when it joins,
  and a human-only verb refuses to sign for a workspace whose genesis differs (`trust.genesis_mismatch`). Devices
  pin it when they pair; the fingerprint is shown then.
- **P1 limit** (§12 O7): the host state dir and the passphrase key file are files the same OS user owns, so an agent
  running as that user can replace them together with the workspace. The pin in the key file stops a swapped
  workspace only as long as the key file itself is the person's.
- **Readers replay authorization, not only signatures.** For every event, in the merged order, a reader checks: the
  actor kind may append the type (§5.2, §5.4); the device certificate chains to a member's person key, has the
  scopes the event needs (§5.3), and was not removed or revoked at that point; `roster_v` is current and the person
  held the role the event needs; an agent's grant was valid, in scope and covered the verb; the policy, generation,
  completeness and source list allowed the decision (§5.7); and the status transition was allowed (§5.9).
- **Rules replay also checks** (C4, #347):
  - The last owner can't be removed or demoted (`members.last_owner`).
  - A viewer can't write, except answers addressed to them and device events.
  - `binds.fields` keys belong to the addon named in the same event.
  - `links.repos` must be in `settings.repos`. A stale repo (one no longer in `settings.repos`) blocks only edits that
    touch links.
  - The initial workspace policies are the §2 config defaults.
  - The merged order is `(ws_seq, at, uid, seq)`. An append that sorts before the last one is refused
    (`chain.bad_ws_seq`).
  - A cross-ticket reference (`parent`, `blocked_by`, `duplicate_of`) must point to a ticket created earlier in the
    merged order.
  - `replay` requires the expected workspace id and refuses a genesis that doesn't match the pin
    (`trust.genesis_mismatch`).
- **Verifier interface.** Signature checks are injected into replay:

  | Method | Arguments |
  |---|---|
  | `verify_person` | `(event, context)` |
  | `verify_host` | `(event, *, log, wsk_pub, workspace_id)`; `workspace_id` never comes from the event |
  | `verify_embedded` | `(event, *, pk_pub, device_cert=None)`; `device_cert` is required for `device.revoked` |

- **Failure.** An event that fails authorization is treated as absent for state, keeps its place in the chain, and
  is reported as `auth.invalid_event`. The host then refuses new person decisions on that ticket (for an invalid
  event in the workspace log: all person decisions in the workspace) until an owner signs
  `invalid.acknowledged {invalid_seq, invalid_head, reason?}` (appended to the log that holds the invalid event) naming it, or a `restore`. The acknowledgement keeps the
  event absent and lifts the freeze; it is in the same log as the invalid event. In P1 an agent with the workspace
  key can cause such a freeze (§12 N4).
- **Derived fields.** `voided` (in `gate.invalidated`), `voided_gates` and `normalised` (in `edit.external`) are
  recomputed by every reader; a mismatch is an authorization failure.
- **What this protects in P1.** An agent that can use the workspace key (core §4) can append agent and host events
  with a valid `host_sig`. Replay stops those events from granting anything a person didn't sign: a forged
  `member.added`, policy, grant, device or decision fails because it needs a person's signature from a device with
  the right scope. It can't stop a forged agent event that cites a real, valid grant, nor host-chosen `ws_seq` and
  `at` that move an event before a revocation; those limits hold until P2 moves the workspace key into the host and
  P3 adds relay checkpoints (§12 N4, N14).

### 5.12 What host events may do

| Host event | Exact effect |
|---|---|
| `branch.pushed` | sets the source-list entry of one repo; raises `verify` and `code` (also on a `done` ticket, which goes to `testing`; there only for a new `sha` on an existing ref) |
| `device.revoked` | appends a revocation that a person key signed (after a `restore`, or for a person with no device left); never one the host made up |
| `gate.invalidated` | records approvals that its cause already voided; raises nothing, changes no status |
| `edit.external` | **installs** the new `body.md` prose as the current text and records its section hashes and references; raises generations per §5.7 (on `done`/`closed`, bound sections are reverted instead) |
| `projection.repaired` | rewrites a projection (`config.json`, `ticket.json`, `keys.jsonl`, a bound section of a `done` ticket) back to what the events say; changes no state |
| `claim.released` | ends a claim for `expired`, `grant_ended`, `member_removed`, `ticket_done` or `ticket_closed`; never for `released` or `handoff` |

Nothing else. A host event never approves, answers, grants, adds a member or creates a ticket.

## 6. Artifacts

Files live in `artifacts/`. The manifest comes from `artifact.added` and `artifact.replaced` events (name, kind,
sha256, bytes, task, ac, label, actor). There is no separate manifest file that gets rewritten.

- **Names** match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`, and are unique per ticket; `artifact.replaced` keeps the
  name.
- **Digests** are `sha256:` + 64 hex, the same form as every other hash (§5.6).
- **Core kinds:** screenshot, log, report, link, dataset, build, diagram, receipt (from `task.done --run`), feedback
  (only a person may add it), other.
- **Addon kinds** are declared in the manifest. An addon artifact has `addon` + `ref` instead of a file.
- **Inline use in prose:** `![alt](artifact:after.png)`. A gated section that shows an artifact binds it through
  its `refs` (§5.8).
- **Evidence:** an acceptance criterion has evidence when a **file** artifact added with a grant or by a person
  names it in `ac`, or a `done` task that `proves` it has a receipt with `exit` 0 whose `commit` equals the current
  source-list `sha` of its `repo` (or whose `repo` is `null`). Addon artifacts and unattended artifacts are never
  evidence (§12 O6). Both kinds of evidence are bound in the verify gate hash (§5.7).

## 7. The ticket document (`orch show --json`)

What the dashboard, the relay and the phone read: `ticket.json` merged with state derived from events, and cached in
`.state/index.sqlite`, which can be rebuilt at any time. The [examples page](https://claude.ai/artifact/6vWgYR8kTxgnPCysfPqpnq)
shows it at four stages.

For agents, `orch show <key>` prints a short text view by default (about 350 tokens):
- header and whose turn it is;
- Current state;
- open questions;
- acceptance-criteria and task summary;
- the last 5 events, as text lines `#seq type actor: detail` (`events` is a list of strings; the actor is `agent`, `person <id prefix>` or `host`; the detail names the task, question, artifact or what changed).

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

**Manifest shapes (P1, minimal).** Validated against JSON Schema `orch.addon/2`; unknown keys are refused.

```json
{
  "schema": "orch.addon/2",
  "name": "estimate",
  "version": "1.2.0",
  "title": "Estimate",
  "entry": {"cmd": ["python", "-m", "orch_estimate"]},
  "fields": {
    "points": {"type": "integer", "min": 0, "max": 100, "set_by": ["owner", "maintainer", "agent"], "gate": ["plan"], "show": true, "filter": true}
  },
  "sections": [
    {"id": "notes", "heading": "Estimate notes", "after": "plan", "gate": ["plan"], "types": ["feature", "bug"]}
  ],
  "artifact_kinds": [{"kind": "chart", "label": "Chart"}],
  "capabilities": ["serve_http"]
}
```

| Key | Shape |
|---|---|
| `name` | `^[a-z][a-z0-9-]{0,39}$`, not a reserved event prefix (§5.4) |
| `version` | semantic version `MAJOR.MINOR.PATCH` |
| `title` | 1–40 characters, no `( ) · :` and no invisible characters |
| `entry` | `{"cmd": [str, …]}`, run out of process (JSON-RPC over stdio) |
| `fields.<name>` | `type`: `string` (one line, `max_len` ≤ 200), `text` (multi-line, ≤ 4 KB), `integer` (`min`, `max`), `boolean`, `enum` (`values`: [token]), `string_list` (`max_items`), `person` (person id); `set_by`: approver tokens, `agent`, `addon` (a human token means the write is a signed person event); `gate?`: [gate]; `show?`, `filter?`: bool |
| `sections[]` | `id` (token), `heading` (1–40 chars), `after` (a core section id), `gate?`: [gate], `types`: [ticket type] |
| `artifact_kinds[]` | `{kind: token, label: str}`; a core kind name is refused |
| `capabilities` | tokens from a closed list: `serve_http`, `spawn_agent`, `pty`, `network`, `git_push` |

The host derives `addon.granted` `binds` (§5.4.2) from `fields.*.gate` and `sections[].gate`/`types`; replay uses
only `binds`, never the manifest.

Deferred to C9: the `needs` expression language, the shapes of `cli`, `skills` and `agents_md`, and addon event
payload schemas. Until then a manifest that has these keys is accepted only with the keys empty.

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
| A3 | **Standing grants.** `orch grant [--hours 8]` (one human signature) signs a grant for the person, the workspace and every verb that isn't human-only. Every session and subagent uses it through `ORCH_GRANT`, and CI gets its own narrower grant. Without a grant, only the low-risk writes `ask`, `log` and `artifact add` work, and they are marked `unattended`. Human-only actions need a human signature every time. Grant terms (D60) and the grant secret: below. |
| A4 | **Parallel subagents.** A ticket has one claim (the manager's). Subagents run with `ORCH_SESSION=<parent>.<sub>` and take a **lease** on one task with `orch task start T3`. Two sessions on the same task are refused. |
| A5 | **The format contract is frozen in P1; some behaviour waits.** Frozen now, with test vectors (§11.5): signed bytes, ids, canonical JSON, hash inputs and their dependencies (generations, `prior`), trust roots and restore. Deferred: addon execution and custom addon events (C9 defines them, P2 runs them), landing (P2), checkpoints on the relay (P3), custody backends other than `passphrase` (P1b and later). |

**Grant terms (D60).** The host enforces them on `grant.issued` and `grant.revoked`:

| Role | Scope | Length | Revokes |
|---|---|---|---|
| owner | `all` or `workable` | 1–24 h | any grant |
| maintainer | `all` or `workable` | 1–24 h | own grants |
| member | `workable` only | 1 h up to `grant_hours` (default 8) | own grants |
| viewer | none | | |

- A grant is always for the person who signs it. `scope: workable` covers the tickets that person may see and work
  on; the host checks it on every claim and write, not only when the grant is issued.
- `verbs` is `"agent"` (every operation whose `who` is `agent` or `unattended`) or a list of operation names (a
  narrower grant, for example for CI), matched exactly; no prefix or group matching. A human-only operation is never in a grant: this is a rule of the model (`HUMAN_ONLY`), so a verb that names one grants nothing even in a `grant.issued` another client wrote (§14.2.9).
- Length is whole hours; `expires_at` = `issued_at` + 3600 · `hours` seconds, all signed. Readers check this,
  `|at − issued_at| ≤ 300 s` and the role terms at the event's position. A `settings.changed` of `grant_hours` doesn't shorten
  existing grants.
- **The grant secret** (§12 N4). `orch grant` draws 32 random bytes and prints `ORCH_GRANT=gr_<ULID>.<b64u
  secret>` once, on the person's own terminal; orch never writes it to a file. orch redacts it from its output,
  logs, events and transcripts it controls, and strips it from the environment of every process it starts (task
  `verify` commands, connection checks), so it reaches the harness and nothing below it. Only `secret_hash` is stored in the event. The host accepts an agent write only with the matching
  secret, so a grant id read from the log is not enough to act under it. Revoking or expiry ends every session
  using it (their claims are released with `grant_ended`).
- `ORCH_SESSION` is `s_<ULID>`, made at session start; a subagent appends `.<n>` (`s_01J9….2`), at most three
  levels.

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
| Edit | `set REF key=value` (keys: title, priority, size, labels, due, links, parent, blocked_by; person-only fields have their own operations), `section set`, `ac add\|edit`, `task list\|next\|add\|start\|done\|skip\|block\|reopen`, `artifact add\|replace\|list`, `log`, `apply --file -` (an atomic batch) |
| Human only | `approve [REF] GATE`, `request-changes [REF] GATE`, `verdict [REF] pass\|fail` (the `REF` comes first, `--ref` still works), `answer`, `close`, `reopen`, `grant`, `member`. Agents get `human_only`, `retry:false`. |
| Admin | `init`, `doctor`, `check`, `instructions sync`, `instructions hook`, `import v1`, `addon …` |

Combined calls for the common loops:

- `orch task done T3 --run --artifact out.png --ac AC2 -m "…"` runs the check, stores the receipt and the evidence,
  and prints the next task.
- `orch submit` moves the ticket to testing only when every acceptance criterion has evidence. Otherwise it lists
  what's missing.

### 10.4 Output and errors

1. stdout carries only the result; stderr carries diagnostics.
2. **Text by default, as short as possible.** Every success template starts with `ok`. For a write the first line is
   `ok <KEY> <event> <detail> seq=<n>`, optionally followed by one `next:` line. A read operation (`status`, `show`,
   `list`, …) puts its body in result lines between the `ok` line and `next:`. A deduplicated retry adds ` duplicate`
   to the first line.
3. `--json` (or `ORCH_OUTPUT=json`) gives `{"v":"orch.cli/2.0","ok":true,"data":…,"key":…,"seq":…,"cursor":…,"hints":[]}`.
4. An error is `{"ok":false,"error":{"code":"conflict.section","message":…,"hint":…,"fix":{"argv":[…]},"retryable":bool}}`.
   The `code` strings are the stable contract.
   - **Text form**, exactly: `err <code> <message> · retry:<bool> · next: <hint> · fix: <cmd>`. The separators are
     fixed. A literal `·` in a value is escaped. `fix` is left out when it equals the hint.
   - **Streams.** The text error goes to stderr. With `--json` the error envelope goes to stdout, as one JSON document.
   - `ok:false` and a non-zero exit code always come together.
5. Exit codes:

   | Code | Meaning |
   |---|---|
   | 0 | ok |
   | 1 | internal error (`not_implemented` too) |
   | 2 | usage or not found |
   | 3 | not allowed (transition or human-only) |
   | 4 | claim, lease or lock |
   | 5 | validation |
   | 6 | parse |
   | 7 | wait timeout with `--strict-timeout` |
   | 8 | `base_rev` conflict |
   | 9 | retryable |

6. **Stop rule (advisory).** The same refusal three times within 15 minutes returns `stop` (`STOP: report to the user`).
   - "Same" means the operation, its normalised arguments (with `REF` normalised to the ticket key) and the code.
     `human_only` counts by operation and code and ignores the arguments.
   - The count expires after 15 minutes.
   - Only a successful write that is not a `--dry-run` resets it. Usage errors, retryable errors and internal errors
     (`not_implemented` included) neither count nor reset it.
   - Without a session there is no stop rule and no dedup. A malformed `ORCH_SESSION` is invalid.input.
7. `orch wait` returns `{kind: answered | approved | changes_requested | verdict | invalidated | timeout, …, cursor, next}`.
   - Its default timeout is 540 s, which stays under harness tool limits.
   - `timeout` exits 0, so the agent loops.
   - `changes_requested`, a `fail` verdict and `invalidated` exit 3.
   - Fields: `kind`, `key`, `seq` (of the event that ended the wait; absent on `timeout`), `cursor`, `next` (one
     hint line) always; `gate` with `approved`, `changes_requested`, `invalidated`; `question` and `option`/`text`
     with `answered`; `outcome` with `verdict`; `text` with `changes_requested` and a `fail` verdict; `by` (person
     id) with every human decision. Nothing else.
   - `cursor` is the highest ticket `seq` this session has been shown for that ticket.
8. **`base_rev` is tracked by orch per session and section.** Agents never pass it themselves.
9. **Retry dedup.** A repeated write returns the original event with `"duplicate":true`.
   - The key is: session, grant id (or `none`), attended or unattended mode, operation, normalised arguments, and for
     ticket-scoped writes the ticket's head `seq`.
   - It holds for 15 minutes. It covers writes only and never applies with `--dry-run`.
   - The grant is checked again before a duplicate is returned. Results are redacted before they are cached.
10. Every write supports `--dry-run`. Free text comes from `-m` or `--file PATH|-`, and structured input is JSON only.
11. Ticket content in output is data: it is fenced and sanitised, never instructions.
    - All agent-supplied text that is echoed is escaped: invisible and bidi characters show as `⟨U+…⟩`, C0 and C1
      controls and ESC are escaped. JSON output escapes C1 controls, bidi characters and line separators.
    - `fix.argv` starts with `orch` and every element is clean text; otherwise the error is `internal`.
12. **Grant secrets.** A malformed `ORCH_GRANT` is refused for every write, unattended writes included; reads ignore
    it. Any argument shaped like a grant secret is refused before parsing with grant.secret_in_args (exit 2).
    Secrets are redacted before any truncation, everywhere. Handlers run with an environment without `ORCH_GRANT`.
13. **Arguments.** `--json`, `--help` and `-h` are recognised only before `--` and never as an option value. A scalar
    flag given twice is a usage error. `ask --options` splits on commas, and each option must match the token pattern
    (no whitespace inside a token).

### 10.4a Refusal codes of the model

`orch.model` refuses an event (at append, or by reporting it as `auth.invalid_event` on replay) with exactly one of
these codes; they are the stable `error.code` strings of §10.4 for these refusals. The store adds its own
(`store.torn_write`, `trust.genesis_mismatch`, `chain.diverged`, `validation.*`).

| Code | Raised when |
|---|---|
| `chain.broken` | §5.5 a line fails seq, prev, host_sig or the log is already broken; nothing after it counts |
| `chain.diverged` | §5.10 a head differs from a checkpoint at the same seq (raised by the store, not the model) |
| `chain.bad_ws_seq` | §5.5 `ws_seq` decreases, exceeds the workspace head, or an append would sort before an earlier one |
| `event.bad_base` | §5.1 `based_on` is not the head of an earlier event of the log |
| `event.duplicate_id` | §5.1 an event `id` repeats in the log |
| `event.bad_actor` | §5.2 the actor kind may not append this type |
| `event.unknown_type` | §5.4 a type not of this log (custom addon events are refused in P1) |
| `auth.invalid_event` | §5.11 derived fields (`voided_gates`) differ from what replay computes; also the label of every refused event on replay |
| `sig.invalid` | §5.3 a person signature is missing or does not verify under the device certificate |
| `members.stale` | §5.4.2 `roster_v` is not the member-list version at the event's position |
| `freeze.active` | §5.11 a person decision while an invalid event is unacknowledged |
| `ack.unknown` | §5.11 `invalid.acknowledged` names no unacknowledged invalid event of this log (seq and head) |
| `trust.genesis_mismatch` | §5.11 the genesis differs from the pinned one |
| `genesis.invalid` | §5.11 the genesis fails its checks, a second genesis, or an event before it |
| `role.denied` | §5.4.2 the signer's role (or ticket role) may not append this type |
| `member.unknown` | §5.4.2 the person is not a member (or is not allowed as owner/addressee) |
| `member.exists` | §5.4.2 `member.added` for a current member |
| `members.last_owner` | §5.4.2 removing or demoting the last owner |
| `device.unknown` | §5.3 the device is not a device of the signer, or does not exist |
| `device.exists` | §5.3 the device id is already registered |
| `device.invalid` | §5.3 the device was removed, revoked, or its certificate expired |
| `device.scope` | §5.3 the certificate lacks `decide` (or `operate`), or has a `drop:` scope |
| `device.cert` | §5.3 an embedded certificate or revocation is not signed by the person key, or is for another person |
| `grant.invalid` | §10.1 the grant is unknown, expired, revoked, ended, another person's, or its person may not run agents |
| `grant.scope` | §10.1 the grant's scope does not cover the ticket, or the person can't see it |
| `grant.verb` | §10.1 the grant's verbs do not name this operation (exact match) |
| `grant.terms` | §10.1 D60 role terms (scope, length) or the time fields are wrong |
| `grant.exists` | §10.1 the grant id is taken |
| `grant.unknown` | §10.1 `grant.revoked` names no grant |
| `quota.unattended` | §5.2 the unattended quota is exceeded |
| `unattended.denied` | §5.2 an unattended event outside what unattended agents may do |
| `settings.invalid` | §5.4.2 two repos resolve to the same path |
| `addon.unknown` | §5.4.2 the addon is not granted or not enabled |
| `ticket.unknown` | §5.4.1 the ticket log has no `ticket.created` yet |
| `ticket.exists` | §5.4.1 the ticket or its key already exists |
| `ticket.frozen` | §5.7 bound edits, `artifact.*`, `task.*`, `claim.taken`, named-role `people.changed` on done or closed tickets |
| `ticket.not_visible` | §9 the ticket is restricted and the signer is not on the list |
| `ticket.bad_reference` | §5.8 a key, acceptance criterion, task or repo reference does not resolve at this position |
| `status.transition` | §5.9 the event is not allowed from the ticket's status |
| `submit.incomplete` | §4, §5.9 submit lacks approvals, evidence, a verification section or branches |
| `people.invalid` | §5.4.1 `people.changed` for `owner` must add exactly one person |
| `conflict.section` | §5.8 `base_rev` does not match the current section or value hash |
| `path.protected` | §5.8 a protected path in `set` |
| `body.unknown_section` | §4 a section the ticket type does not have |
| `body.unknown_artifact` | §5.8 a reference to an artifact that is not in the file manifest |
| `repo.unknown` | §3 `links.repos` names a repo missing from `settings.repos` |
| `restore.bad_head` | §5.10 `restore` does not name the last event on disk |
| `gate.stale` | §5.7 generation, gate hash, policy hash or source list is not current |
| `gate.no_eligible` | §5.7 the effective policy leaves no approver token |
| `gate.not_eligible` | §5.7 the signer may not decide this gate (token, `not`, independence) |
| `gate.incomplete` | §4 the gate's sections, acceptance criteria or tasks are missing |
| `gate.not_applicable` | §5.7 the gate does not apply to the ticket type |
| `gate.status` | §5.9 decisions are not accepted in the ticket's status |
| `gate.invalidated_mismatch` | §5.11 `voided` differs from the approvals the cause retired |
| `policy.invalid` | §5.7 a code policy without `not: assignees` and `independent` |
| `source.missing` | §5.7 a linked repo has no observed branch |
| `source.unlinked` | §5.7 the repo is not in `links.repos` |
| `source.not_new` | §5.7 `branch.pushed` repeats the projection, has a wrong `before`, or changes a ref on a done ticket |
| `claim.not_live` | §5.2 no live claim of that session (or takeover of nothing) |
| `claim.exists` | §5.4.1 the ticket is claimed and no takeover was given |
| `claim.not_holder` | §5.4.1 an agent acts under another session's claim |
| `task.unknown` | §5.4.1 the task id is not in `ticket.json` |
| `task.leased` | §10.1 A4 another session holds the task lease |
| `task.state` | §5.4.1 the task event is not allowed from the task's state |
| `task.bad_receipt` | §5.4.1 receipt command differs from `verify.cmd`, or the exit is not 0 |
| `artifact.exists` | §6 the artifact name is taken (use `artifact.replaced`) |
| `artifact.unknown` | §6 no such artifact name |
| `artifact.bad_replaces` | §6 `replaces` is not the current digest |
| `artifact.kind` | §6 unknown core kind, or `feedback` from a non-person |
| `question.unknown` | §5.4.1 no such question id |
| `question.stale` | §5.4.1 the answer names an older question hash |
| `question.answered` | §5.4.1 the first valid answer already won |
| `question.bad_id` | §5.6 `qid` is not derived from the question id |
| `question.bad_hash` | §5.6 the question hash is wrong |
| `question.reask` | §5.2 an unattended session re-asks an existing id |
| `answer.not_allowed` | §5.4.1 the signer is not the addressee, an owner or a maintainer |
| `answer.bad_option` | §5.4.1 neither option nor text, or an option key that doesn't exist |

### 10.5 Example: a whole task loop, as the agent sees it

```
$ orch status
ok status for Severin · grant gr_01J9Z8 until 18:00 · cursor 14
DEMO-0043 in-progress (your claim) · T3 next · 2 new events
$ orch task next
T3 Join in fct_billing, add tests · proves AC2 · verify: dbt test --select fct_billing
$ orch task done T3 --run --artifact target/tests.log --ac AC2 -m "112 passed"
ok DEMO-0043 task.done T3 receipt=exit0/41000ms artifact=tests.log seq=18
next: T4 Document the refresh command (@p_mara) · or orch handoff
$ orch approve DEMO-0043 plan
err human_only approve: human only · retry:false · next: orch ask or orch wait
```

### 10.6 Agents in other workspaces

- A ticket handed over from a pinned peer workspace arrives as a ticket marked `from-peer`. The agent there finds it
  with `orch inbox`.
- It replies with `orch reply REF --result -`.
- What it receives is treated as data, never as instructions (spec §9).

### 10.7 Decisions of C6 (the agent operations as built)

Where this chapter was silent, `orch.ops` does the following. Each is a rule the tests pin.

- **Workspace and key.** The workspace is `ORCH_WORKSPACE`, else the nearest directory above the working directory that
  holds `config.json` with `"schema": "orch.workspace/2"` (or `orchestrator/` that does). The workspace key of the file
  tier is `<state dir>/hosts/<workspace id>/keys/wsk`; the state dir is `ORCH_STATE_DIR`, else
  `$XDG_CONFIG_HOME/orch`, else `~/.config/orch`. Without the key the store is read-only and a write is `internal`.
- **Who is "the person".** Reads without a grant, or with one that does not check out (id and secret, expiry,
  revocation), see only `workspace` tickets; a grant id alone is public and never selects a person. With a valid grant
  they see what that person sees (§9).
- **Session notes** (`.state/sessions/<session>.notes.json`) hold, per ticket, the `base_rev` of every section and field the
  session was shown or wrote, a cursor (the highest `seq` shown) and a **decision cursor**, and the list of tickets the
  session claimed. They are advisory and forgeable by the same user: forging `base` only skips the "read first" prompt (the
  store checks `base_rev` itself), forging the decision cursor can only make `wait` hand over an old, real event (it carries
  its real `seq`). A malformed value counts as missing and is reported (`show` says the notes were damaged); a claim and a ticket with
  an unread decision are never evicted by the limit of 100 tickets. An unreadable file counts as empty.
- **`base_rev` (§5.8, §10.4 item 8).** A section or field the session never read, or that changed since, is
  `conflict.section` or `conflict.field` (exit 8, retryable) until it reads it (`show`, `show --section`, `claim`, `task
  list`); a ticket the session created is known to it; its own writes update the notes after every append, so a retry after
  a crash is not a conflict with itself.
- **Decisions are never lost.** A decision is an answer, an approval, a change request, a verdict or an invalidation. Only
  `wait` moves the decision cursor, one decision per call (an explicit `inbox` hands over all and moves it too). `show`
  lists the decisions after the cursor in the ticket block (`UNREAD #4 answered Q1 option=b: text`, change requests and
  failed verdicts with their text), `show --log` prints their content, and `inbox` lists them. A ticket the session never
  touched starts its cursor at the head of its first touch (older decisions were not made for it). `wait` of a done ticket
  still works.
- **Claims.** The session's notes list the tickets it claimed; a command without a REF loads those (verified) and nothing
  else, `s_X.1` reads `s_X`'s list. A second `claim` is refused with `claim.held` unless `--also`, and with several claims
  every REF-less command is `ambiguous_ref` (its hint names a real key). `s_X.<n>` works under the claim of `s_X`, but only
  `s_X` releases it or hands off (the event schema binds `session` to the actor). A lapsed claim is taken over
  (`--takeover --reason`), also by its own session; the host does not record the lapse by itself yet.
- **Grant verbs are operation names, matched exactly (§10.1).** The CLI refuses an operation the grant does not list with
  `grant.verb` (exit 3, its own code); `apply` needs `apply` and every item's operation. The model checks again: an agent
  event must be emitted by some granted operation (the table operation -> event types comes from the registry and is passed
  in; an unknown name grants nothing; `"agent"` covers every agent operation and never a person's event). So `["task.done"]`
  runs `task done --run` (its receipt's `artifact.added` is in `task.done`'s emits) and nothing else. The table is a
  **frozen, versioned constant of the format** (`orch.model.emits.EMITS_V1`, digest pinned by a test; a test also checks
  that the live registry equals it): replay never reads the registry. A change of any operation's `emits` adds
  `EMITS_V2` and bumps `CURRENT`; logs written under version 1 keep replaying under `EMITS_V1`.
  **Before any `EMITS_V2` exists** (a rule of §10.1 too): the table version a grant is judged by must be readable from the
  log (a field on `grant.issued`, or the workspace format version at the time it was issued), never from the running
  release; replay that picked `CURRENT` would change the meaning of old logs.
- **Receipts.** A receipt means "this command exited 0 in the agent's environment, in this working copy, at this commit": it
  is **attested by the agent's environment**, not independent verification (the agent controls `PATH`, may pick among the
  linked repositories by its working directory, and a ticket with no linked repository gives `repo: null`, which counts as
  evidence; `verify.cmd = "sh -c '...'"` runs a shell, orch adds none). The control is that `verify.cmd` sits in the plan a
  person approves; the P2 host runs it itself. `task done --run` runs the ticket's `verify.cmd` split into arguments, in
  its own process group with a hard timeout (a process that leaves the group with `setsid` survives: without a separate
  user or job object nothing stops it), stdin closed, 1 MiB of output kept and the rest dropped, an **allow-list**
  environment (`PATH`, `HOME`, `LANG`, `LC_*`, `TMPDIR`, `TERM`, plus the names the ticket's skills declare, D56, none yet),
  so no grant, token, `ORCH_*` or `GIT_*` variable reaches it. git is asked with a scrubbed environment and
  `core.fsmonitor=false`: HEAD is read before and after (a moved repository is `verify.failed`), and so is the state of the
  tree (`git status --porcelain` with untracked files, and `skip-worktree`/`assume-unchanged` flags from `git ls-files -v`):
  a tree that **looks dirty** gives `receipt.commit: null` and the output says so; a receipt without a commit is never
  evidence for a task with a repo. This is **best effort** against accidental changes: ignored files and a `filter` in the
  repository's own config can hide a change, and a receipt is agent-attested anyway. The output is the `receipt` artifact
  `<task>-receipt.log` (its digest is the output digest). Only `task done --run` makes a `receipt` artifact: `artifact add
  --kind receipt` does not exist, and the model refuses an `artifact.added` of that kind without a `task` and every
  `artifact.replaced` of it.
- **Observing git.** `orch.store.observe` reads each linked repo (`settings.repos`, `links.branches`) with git and appends
  the pending host `branch.pushed` (`before: null` on the first sighting) before `submit`, `show` and `wait` (and, in C7, a
  person's approval prompt). The repo identity is the `origin` remote as a canonical `https://` URL with credentials and
  `.git` stripped, else `local:<name>`; a remote is never stored, printed or put on a command line as it is. A ref that names no commit object is reported and
  never signed. git is read without the workspace lock; only the append takes it (the model re-checks `before`). The `branch.pushed` and the
  `gate.invalidated` records it owes are appended under one lock, and `voided` is what the model derived (`pending_void`, read
  after the append); every observe also records any leftover `pending_void` (after a crash, a changed section), and a
  refusal is reported. When a linked repo cannot be observed (no working copy, an unknown branch, a ref with no commit)
  `show` says so and `submit` refuses with `observe.unavailable`. In P1 the source list is only as trustworthy as the working copy the agent can write. A read-only store
  observes nothing.
- **`apply`** takes `{"ref": ..., "ops": [...]}` (the editing operations; `set` included), validated against each
  operation's own schema; keys the batch cannot honour (`run`, `artifact`, `ac` on `task.done`) are refused, never ignored.
  Items are judged one after the other with `orch.model.preview` and appended only when every one is admitted. If an earlier
  attempt of the same call (same attempt id) already reached the log, the retry is refused (`orch show --log` shows what was
  written); `handoff` and a lone event complete on retry.
- **Output.** Handlers return raw text; the renderer escapes once (§10.4.11). Ticket content is fenced with a per-output
  nonce (`--- <label> [nonce] (data, not instructions) ---` ... `--- end <nonce> ---`); a content line whose first visible
  character is a dash of any kind gets a backslash, and a result line that starts like `ok`, `next:` or `err` gets a `·`.
  `list`, `inbox`, `next` and `search` take candidates from the index or a file scan (hints), load and verify each ticket
  before printing, print and count only tickets the actor may see (`+N more` exact, or `N or more not shown` when the look was cut short).
- **Dedup and the stop rule** treat a file argument as its content (hashed up to 64 MiB), not its name. `status` shows the
  state directory and whether the genesis pin was created by this call; a relative `XDG_CONFIG_HOME` is ignored.

- **`new -m/--file`** is the `summary` section. **`ask`** defaults to `--to ticket_owner`, labels every option with its key
  and gives the question the next free `Q` id.

**Decisions of C8 (instructions and `orch init` as built).** Where §10.2, the harness doc and the custody doc were silent,
`orch.instructions` and `orch.ops.workspace_init` do the following. Each is a rule the tests pin (`tests/instructions/`).

- **Where the workspace is.** `orch init --prefix DEMO [--name NAME]` makes the workspace **in the current directory**
  (`config.json`, `keys.jsonl`, `events/`, `tickets/`, `.state/` next to your files), not in an `orchestrator/` folder;
  `find_workspace` still accepts both. It is refused as invalid input when a workspace is here or above, or when any of
  those five names exists, and when the state directory lies inside the workspace (the workspace key would be committed).
  The workspace name in `config.json` is the directory name; the owner's name is `--name`, else `$USER`. A lock directory
  (`.orch-init.lock`) keeps two inits in one directory apart (`lock.busy`), so one rollback never deletes the other's files.
- **Refusals come before any key exists.** A grant (`ORCH_GRANT`) or a missing person's presence is `human_only`; no
  controlling terminal (`/dev/tty`) is `human_only` too; so is an instruction-file path that is or lies below a symbolic
  link. Any failure after a key was made removes the key directory and the workspace files this call made, so a second try
  starts clean. Ctrl-C and SIGHUP (closing the terminal) do the same and print one line (`stop: init cancelled, nothing
  was created`), no traceback. Key directories of an init that was killed (SIGKILL, power loss) carry an
  `.init-incomplete` marker with the process id; the next `init` removes those whose process is gone. Every failure after
  a passphrase or the code was shown says that what was shown is void.
- **The passphrase comes first.** `/dev/tty` shows a generated passphrase (six distinct words of the BIP-39 list, about
  66 bits, shown once); the person types it back to confirm, or types one of their own instead (asked twice). Only then
  is the recovery code made. Three tries, then `init` stops with nothing created.
- **The recovery code is confirmed and then wiped.** The 24-word code is shown on `/dev/tty` and nowhere else (never
  stdout, stderr, a file or the result), the person writes it down and types **three words from random positions** back
  (echo off); a wrong word shows the code again, three failures stop the init. Then the screen and the scrollback are
  cleared (`ESC[3J ESC[2J ESC[H`; a terminal that ignores it keeps its scrollback, which is stated, not hidden).
- **The person key** derived from the code signs the first device certificate and the workspace key's delegation and is
  then dropped: **nothing on disk holds it**. This is a P1 deviation from D50 and §5.1/§5.2, which keep it wrapped by a
  Secure Enclave key on the primary device (a stored copy under a passphrase would be a file every agent of the same OS
  user can read); it is recorded in orch-v2.md §5.1 and portable-custody D65 pending the owner's confirmation, and is
  revisited with the `secure-enclave`/`tpm` backend. The consequence: adding or revoking a device, or issuing the
  key-exchange certificate of P3, needs the code, typed on `/dev/tty` with echo off, never from an argument, stdin or the
  environment. The label of the first device is sealed with an HKDF of the person key's scalar; the certificate's
  `dk_kx_pub` is a fresh key whose private half is discarded (P1 has no key-exchange user; the relay is P3).
- **The device key** is a `passphrase` backend key (`dk`, role `device`) at
  `<state dir>/hosts/<workspace id>/person/dk.key.json` (where the human operations of C7 read), made from the passphrase
  the person settled on; the backend asks it once more when it signs the genesis, showing the signed fields (§5.3). The
  workspace key is the `file`-tier key `keys/wsk` of §10.7. `Store.append` writes `config.json` and `keys.jsonl` and pins
  the genesis in the state directory (§5.11). **P1 deviation:** the genesis is not also recorded in the device key file
  (§5.11 "custody key file"): the file's authenticated header is fixed and its passphrase is needed to change it; the pin
  in the state directory is the only record until pairing (P3) adds one.
- **Passphrase strength** (C7 and C8 security reviews; `orch.custody.strength`, checked by the passphrase backend on every
  key it creates, so it holds for `init` and any later device key): at least 14 Unicode scalars after NFC, not on the
  vendored list of the 5000 most common passwords (also with leet substitutions), and an entropy estimate of at least
  60 bits, where the estimate is the cheapest parse of the text into common words (12 bits each), ascending, descending
  and keyboard runs (5 bits), repeats (1 to 2), years (7) and single characters (log2 of the character pool). The
  generated six-word phrase (about 66 bits) is judged by the same rule.
- **Files written, never through a link.** `AGENTS.orch.md` (workspace root), the three built-in skills in
  `.claude/skills/<name>/` with their `orch.skill.json` (scope `builtin`), one line `Before working on tickets, read
  AGENTS.orch.md (orch).` in `AGENTS.md` (Codex and others), `@AGENTS.orch.md` in `CLAUDE.md` (Claude Code) and `.state/`
  in `.gitignore`, each appended to an existing file and created otherwise, never duplicated. No file or directory on the
  way (`.claude`, `.claude/skills`, the skill folder, the file) may be a symbolic link: `init` refuses before any key is
  made, `instructions sync` writes nothing (invalid input). Reads open with `O_NOFOLLOW`; temporary files come from
  `mkstemp` in the target directory (exclusive, random name). A skill whose sidecar names another scope than `builtin`
  is the owner's and is kept, and `init` and `sync` say so (`kept .claude/skills/orch-tickets (scope workspace ...)`). A
  Claude Code user who also installs the plugin sees each skill twice (the plugin's and the workspace's); the plugin
  layout (`plugins/orch-core/skills/`, `hooks/hooks.json`) is generated by `scripts/sync-plugin.sh` and kept equal to the
  generator by a test. If writing the files fails after the genesis, the workspace stays and the result says so and what to
  run (`orch instructions sync`; the `AGENTS.md`/`CLAUDE.md` lines are added by hand).
- **`AGENTS.orch.md`** is a template whose commands are registry markers and whose example calls come from
  `orch.ops.workflows`: renaming or removing an operation fails the generator, not a reader. The first line is
  `orch v2.0 (instructions rN) · ...`; `N` is `INSTRUCTIONS_REV`, raised whenever this text or a skill changes (each skill's
  text is pinned to its `skill_version` by a hash in the tests). Addons may add one line each before the last line, within
  the 25. It does not promise `artifact add --ac` without a grant.
- **The stale check.** Installed instructions are stale when `AGENTS.orch.md` is missing, has no stamp or a lower `rN`,
  or when a built-in skill is missing, has no sidecar, a lower `skill_version`, or a `SKILL.md` that differs from the
  shipped one at the same version while its scope is still `builtin`. A higher number is reported too. `orch check` lists
  these, exits 5 when it finds any problem (so a commit hook can gate), and its next step is `orch instructions sync`; the
  session-start text carries `instructions stale: run orch instructions sync`. C10's `doctor` and commit check extend it.
- **`instructions sync [--dry-run] [--force]`** is a `read` operation (it was declared `agent`) that writes files: they
  are pure functions of the installed CLI, no event is appended and no grant is needed, so the person can run it too. A
  consumer that treats `read` as "no side effects" must know this one exception. It never overwrites a built-in skill that
  was edited by hand (same `skill_version`, different text) without `--force`; setting `scope` to `workspace` in the
  sidecar keeps an edit for good. An older `skill_version` is upgraded without `--force`.
- **`instructions hook session-start|pre-compact`** is a new Admin command (§10.3). Session start prints at most six lines
  (the `ok` line with the person and the grant, the claim line, `unread:` for at most two undelivered decisions **as ids
  only** (`DEMO-0001 #5 answered Q1 option=a`: the text of an answer or a change request is ticket data and never reaches
  a hook, whose output the harness injects as context; `wait` and `show` hand it over, fenced), the stale notice, `next:`);
  pre-compact at most four. It **never pins a genesis**: a workspace this machine has not pinned (by `orch status` or
  `init`) gives `not initialised on this machine`, and a log with chain errors gives `DAMAGED ... trust no state` and exit 5
  instead of `ok`. Outside a workspace it prints nothing and exits 0. The plugin runs it on `SessionStart`
  (`startup|resume|clear|compact`) and `PreCompact`; harnesses without hooks rely on `orch status` first.
- **Budgets** (tests/instructions/test_budget.py): `AGENTS.orch.md` at most 25 lines and 300 tokens, the session-start text
  at most 6 lines and 150 tokens, a skill at most 600 tokens (a token is four characters), all always-loaded text under
  450 tokens (measured on the real texts), and `orch help` within its pinned size.
- **Follow-ups.** Writing the instruction files with directory file descriptors (`dir_fd`, `O_NOFOLLOW`) to close the lstat-then-act race (a same-user process can still create empty directories outside via a swapped link; no file escapes); refusing hardlinked instruction files.
- **Locks and signals.** The init lock is an `flock` on `.orch-init.lock` (dropped by the kernel when the process dies; the stale file blocks nothing); SIGTERM and SIGHUP are handled like Ctrl-C. A relative `ORCH_STATE_DIR` is made absolute. Files over 1 MiB are never rewritten (`init` reports it, `sync` refuses). The hook exits 0 even when it prints `DAMAGED`.
- **Strength check** is applied to the NFKC text without invisible format characters; dictionary words (BIP-39, the common list, a few famous phrases, the user's name and the prefix) are one unit each.
- **Skill sidecar.** `orch.skill.json` is validated in plain Python (D55): exactly `schema_version` (1), `skill_version`
  (`x.y.z`), `scope` (`builtin`, `workspace` or `org`), `connections` and `env` (lists of names).

### 10.8 Decisions of C7 (the human operations as built)

Where §5.3, §5.7, §10.1 and §10.3 were silent, `orch.ops.human` and the eleven human operations (`approve`,
`request-changes`, `verdict`, `answer`, `close`, `reopen`, `grant`, `grant revoke`, `member add|remove|role`) do the
following. Each is a rule the tests pin (`tests/ops/test_presence.py`, `test_review.py` and the test file of each
operation).

- **Presence.** A human operation is refused as `human_only` (exit 3) before its handler runs when `ORCH_GRANT` is
  present in the environment, empty or not (§10.3: "Agents get `human_only`"), and the handler refuses again if a grant
  ever reaches it. Every signature then asks for the passphrase on `/dev/tty` through the passphrase backend (§5.3, D65):
  never stdin, stdout, stderr, an argument or an environment variable, and no option skips or carries it. No controlling
  terminal is custody.no_prompt, a wrong or empty passphrase is custody.wrong_passphrase, a missing key file is
  custody.no_key (hint: `orch init`, C8); all exit 3, not retryable, declared by every human operation and kept distinct
  (D65: agents branch on them). Nothing is written in any case. A harness that shares the person's terminal can still
  trigger the prompt; that limit is D65's. A person's operation is never deduplicated or answered from the session
  records: every call is a fresh signature.
- **Who signs.** The key is `<state dir>/hosts/<workspace id>/person/dk.key.json` (key id `dk`, passphrase backend,
  role `device`), next to the workspace key. The person and the device are not named by the caller: the device id is
  derived from the key file's public key and looked up in the replayed device roster, which says whose device it is. A key
  the log does not know, a removed or revoked device and a person who is no longer a member sign nothing
  (role.denied, before any prompt). P1 holds one device key per workspace per machine; creating it is `orch init`/`keys`
  (C8), not C7.
- **What is signed comes from the log.** `gate`, `gate_gen`, `hash`, `policy_hash`, `source_sha`, the question `hash`,
  `roster_v` and `based_on` are read from the verified state at the moment of the call; the operations have no option
  for any of them. The ticket is named with `--ref` (a person has no claim, so there is no "my claim" default:
  ambiguous_ref); a ticket the signer may not see, and a key that does not exist, give the same not_found (§9).
- **The review (§5.7: "the prompt shows the text").** For `approve`, `request-changes` and `verdict`, before the
  passphrase prompt, orch writes the gated content to `/dev/tty` exactly as the gate hash binds it and asks the person to
  type the ticket key and Enter (anything else, end of input, Ctrl-C: nothing is signed; no flag or variable skips it;
  no terminal is custody.no_prompt): the ticket key (derived by orch from the log id, labelled as not signed) and the log id, then the gate's section
  texts (each checked against its hash), acceptance criteria, tasks with their verify commands, links, the source list
  and the artifact names with digests, then the short gate hash (the first 12 hex digits, 48 bits, of the verified gate hash). `orch show`
  prints the same 12 digits per open gate (`plan:open#8163ab12cd34`), and `show --full` prints the ticket's log id, so a person can compare
  what they read earlier with what they sign. Ticket text is data: every line is escaped and starts with `| `; orch's own headings and
  labels are printed bare, so text cannot pass for one. The review opens with a summary line (lines per section, number
  of criteria and tasks) and, for `verify`, lists the task receipts (task, commit, exit code); it says that addon fields
  and packages, the policy and people lists and earlier approvals are bound by the gate hash but not shown. There is no
  pager in P1: a long text scrolls, which is why the summary comes first and the confirmation comes after the content.
  Input typed before the content appeared is discarded (`tcflush`) and confirms nothing. git runs in its own session
  (no controlling terminal), so a repository's filter cannot reach the person's terminal. If the content changes while
  the person reads or types, the append is refused as gate.stale. The decisive fields of the
  signing bytes follow in the passphrase prompt (D41).
- **Artifact bytes (§5.7).** Before the review, the SHA-256 of every bound artifact file (`requirements`/`plan`: those
  named in the section refs; `verify`: the whole manifest) is recomputed; a missing, replaced or changed file is
  artifact.mismatch and nothing is shown for signing.
- **Judged before the prompt, the lock not held while typing.** The event is judged with `orch.model.preview` as
  `Store.append` will judge it (all rules, the signature aside), so a refused event costs no passphrase. The workspace
  lock is not held while the person types; `Store.append` then verifies the signature and judges again, so a change
  during the prompt is gate.stale or members.stale and the person decides again.
- **A person counts once per generation.** A second `approve` (or `verdict pass`) by the same person at the same gate
  generation is refused as gate.already_approved before the prompt. The model counts a person once whatever the policy's
  `count` (`generations.counting`), so with `count` 2 the second approval has to come from another person.
- **D58/D59 binding.** `verdict` and `approve code` read git (`orch.store.observe`, which appends any owed
  `branch.pushed`) right before the prompt and bind the commits it finds as `source_sha`. Each linked working copy must be
  on the bound branch (`refs/heads/<branch>`), at the bound commit, with nothing uncommitted; otherwise
  observe.unavailable. The decision binds a commit, so the files a person ran or read must be that commit (F1 was silent;
  a detached HEAD elsewhere and another branch are refused). The same check runs again inside `Store.append` with the
  workspace lock held (a `precommit` callback, read-only), after the passphrase: a source that moved meanwhile is
  gate.stale and nothing is appended. A linked repository that cannot be observed is observe.unavailable, a missing ref
  source.missing (§5.7). A ticket that links no repository signs `source_sha: []`.
- **`--dry-run` writes nothing:** it does not observe (no `branch.pushed`), does not prompt and does not sign; it judges
  with what the log holds and reports a source list the log has not recorded yet as observe.unavailable. `grant --dry-run`
  makes no grant and prints no id and no hint.
- **`grant`.** The 32-byte secret is drawn when the command runs, only its hash goes into the event, and
  `ORCH_GRANT=...` is written to `/dev/tty` **after** the append and nowhere else: not stdout (a pipe, a log, an
  agent's transcript), not stderr, not a file, not a record. The terminal keeps it in its scrollback, which a process
  that can read the terminal (a multiplexer, D65) can read: orch says so and asks the person to clear the scrollback once
  the harness has the value. If the terminal cannot show it the command fails with custody.no_prompt and names the grant
  to revoke. Defaults: `--hours 8`, `--scope workable`, `--verbs agent`; `issued_at` is the clock when the command
  starts (readers allow 300 s, §10.1). **`--verbs` is validated at issue:** each name must be an operation an agent or an
  unattended agent may run (§10.1: a human-only operation is never in a grant; the model would otherwise accept a name
  that grants nothing). The D60 role terms are the model's.
- **`member add`** takes what the invitee made: `--pk` (their person key, base64url) and `--cert` (their first device
  certificate, a path or `-`); the person id is derived from the key and the certificate is checked by the model
  (device.cert). The output tells the owner to compare the invitee's person id out of band: whoever relays the
  invitation can substitute their own key. `member role` and `member remove` of the last owner are invalid.input carrying
  members.last_owner. Maintainers add members and viewers, owners do everything else; a device whose certificate lacks
  `operate` cannot sign member, role, grant or settings events (device.scope, §5.3).
- **Texts.** `request-changes` and a `fail` verdict need `-m`; every text passes the §11.3 rules (parse.text) and the
  grant-secret filter. `close --duplicate-of` goes through the same visibility check as `--ref`.
- **Not built here, because no operation is declared for it in §10.3:** `invalid acknowledge`, `restore`, ticket
  `policy`/`people`/`visibility`, workspace `policy`/`settings`, `device` and `addon` events. Their person events exist
  in §5.4; their operations arrive with the task that declares them.

## 11. Encodings, ids, text and value lists

### 11.1 Field types and ids

Examples in this document shorten ids (`p_sev`, `01J9ZP…`). The real forms:

| Type | Form |
|---|---|
| ULID (`uid`, event `id`) | 26 characters, Crockford base32 upper case: `^[0-7][0-9A-HJKMNP-TV-Z]{25}$` |
| key | `<prefix>-<n>`: `n` ≥ 1, zero-padded to 4 digits, more digits only when needed (`DEMO-0043`, `DEMO-12345`; `DEMO-00043` is refused). A reference `43` means `DEMO-0043`. |
| prefix | `^[A-Z][A-Z0-9]{0,15}$` |
| `workspace_id` | 32 lower-case hex (protocol §2.2, §4), in `config.json` too |
| person id | `p_` + the protocol `person_id` in 32 hex (derived from the person key, protocol §4) |
| device id | `d_` + the protocol `device_id` in 32 hex |
| host id | `h_` + ULID |
| session id | `s_` + ULID, then at most three `.<n>` with `n` in `^[1-9][0-9]{0,3}$` |
| grant id | `gr_` + ULID |
| agent id | the harness name, `^[a-z][a-z0-9-]{0,31}$` (`claude-code`, `codex`, `ci`) |
| AC, task, question id | `AC`, `T`, `Q` followed by `^[1-9][0-9]*$` |
| `qid` | 32 lower-case hex, derived from the question id (§5.6); the protocol §13 `question_id` |
| section id | a core id (§4), or `<addon>.<token>` |
| token | `^[a-z][a-z0-9_]*$` |
| timestamp | `YYYY-MM-DDTHH:MM:SSZ`, UTC, seconds `00`–`59` |
| date | `YYYY-MM-DD` |
| hash | `sha256:` + 64 lower-case hex (§5.6) |
| git commit id | the full id: 40 lower-case hex (SHA-1 repos) or 64 (SHA-256 repos), never abbreviated |
| b64u | canonical unpadded base64url (protocol §2.2) |
| signature (`sig`, `host_sig`) | b64u of the 64-byte P-256 signature `r ‖ s` (protocol §1.1, suite 2). No `p256:` prefix: the suite is in the signed bytes. |
| public key | b64u of the 65-byte uncompressed point |
| signed object | protocol §2.4 `{"o": …, "sig": …}`, verbatim |
| str | text under §11.3, non-empty, at most 4 096 bytes (UTF-8) unless the field says otherwise; one line unless it is prose. Character limits count Unicode scalar values; size limits count UTF-8 bytes. |
| repo identity | §5.7: canonical `https://` remote URL, or `local:<repo name>` |
| source list | `[{"repo": repo identity, "ref": str, "sha": git commit id}]`, sorted by `repo` |
| int | an integer in `±(2^53 − 1)` |

### 11.2 Canonical JSON and limits

- `cj` and strict parsing are orch-relay protocol §2.3, unchanged: no floats, ASCII keys, safe integers, Unicode
  scalar values, duplicate keys refused.
- **Depth:** the root container is level 1. 16 nested containers are allowed, 17 are refused. (A depth vector goes
  into the protocol vectors.)
- The root of every file and every line is an object.
- `ticket.json` is pretty-printed for git, but everything hashed or signed is `cj` of the strictly parsed object.
- Limits: 262 144 bytes per `ticket.json`, 65 536 bytes per section, 524 288 bytes per event line; the store
  refuses larger input before parsing it.

### 11.3 Text rules

Every string in `ticket.json`, `body.md` and events:

- is UTF-8, NFC, and uses LF line endings. On the way in, the store converts CRLF **and lone CR** to LF, then applies
  NFC. Nothing else is changed: trailing spaces, a missing final newline and blank lines are kept and hashed.
- refuses (on write, `validation.text`): C0 controls except LF and TAB, DEL, C1 controls (U+0080–U+009F), bidi
  controls (U+202A–202E, U+2066–2069, U+200E, U+200F, U+061C), and code points unassigned in **Unicode 16.0**.
- **Unicode is pinned to 16.0** for NFC and for "unassigned". A runtime with other Unicode data (Python 3.11 has
  14.0) uses bundled 16.0 tables. A change of the pinned version is a new `hash_v`.
- keeps other invisible characters but shows them as `⟨U+…⟩` in approval prompts and `orch show` (§5.7).
- One-line fields (titles, labels, names, option labels, reasons) contain no LF.
- No floats in anything signed or hashed (use `ms`, `cents`). Timestamps per §11.1.
- Hash and signature functions refuse a string that breaks these rules; they never normalise (§5.6).

### 11.4 Value lists

| List | Values |
|---|---|
| ticket type | `feature`, `bug`, `chore`, `spike`, `epic` |
| priority | `low`, `medium` (default), `high`, `urgent` |
| size | `xs`, `s`, `m`, `l`, `xl`, or `null` |
| status | `backlog`, `open`, `in_progress`, `testing`, `done`, `closed` |
| member role | `owner`, `maintainer`, `member`, `viewer` |
| ticket people (T4, `people.changed`) | `owner`, `assignees`, `reviewers`, `watchers` |
| approver token (policies) | `owner`, `maintainer`, `member`, `ticket_owner`, `assignees`, `reviewers`, `watchers` |
| question `to` role | `ticket_owner`, `assignees`, `reviewers`, `watchers` |
| gate | `requirements`, `plan`, `verify`, `code` |
| gate `applies` | `all`, `off`, or a list of ticket types |
| verdict outcome | `pass`, `fail` |
| close resolution | `wont_do`, `duplicate`, `obsolete`, `other` |
| claim release reason | `released`, `handoff`, `expired`, `grant_ended`, `member_removed`, `ticket_done`, `ticket_closed` |
| `gate.invalidated` cause | `new_commits` (D58), `content_changed`, `policy_changed`, `member_changed`, `device_compromised`, `conflict_resolved` (D53, P2) |
| `device.revoked` reason | `compromised`, `lost`, `retired` |
| device scope levels needed | `decide` for every person event; also `operate` for member, policy, settings, grant, addon and `restore` events; `drop:` certificates never |
| `projection.repaired` cause | `external_edit`, `projection_mismatch`, `keys_mismatch` |
| `auth` | `passphrase`, `secure-enclave`, `secure-enclave-unlocked`, `webauthn`, `tpm`, `windows-hello` |
| grant scope | `all`, `workable` |
| grant verbs | `"agent"` or a list of operation names |
| core artifact kind | `screenshot`, `log`, `report`, `link`, `dataset`, `build`, `diagram`, `receipt`, `feedback`, `other` |
| manifest field type | `string`, `text`, `integer`, `boolean`, `enum`, `string_list`, `person` |
| addon capability | `serve_http`, `spawn_agent`, `pty`, `network`, `git_push` |

### 11.5 Frozen in P1, and the test vectors

Frozen with F1 (a change is a new `hash_v` or a new signed-event `contract`): the signed bytes (§5.3, §5.5, §5.10), the
ids (§11.1), `cj` and the text rules (§11.2, §11.3), every hash input and what it depends on (§5.6, §5.7, including
generations and `prior`), the trust root (§5.11) and restore (§5.10).

Vectors to add to orch-relay `vectors_v2.json` and to the core tests, before C1 is merged again:

| Vector | Covers |
|---|---|
| `labels` | the new labels of §5.6, prefix-free with the protocol's |
| `canonical_json` `depth_16`, `depth_17`, `minus_zero` | depth counting, `-0` → `0` |
| `text` | CRLF and lone CR → LF, NFC (Unicode 16.0), refused controls, bidi and unassigned code points |
| `section_text` | heading parsing, leading/trailing LF trimming, trailing spaces kept |
| `gate_hash` | one known answer per gate (`requirements` with an inline artifact, `plan` with tasks and `prior`, `verify` with receipts and a source list, `code`) |
| `policy_hash`, `people_hash`, `question_hash`, `value_hash`, `section_hash` | one known answer each, plus a refused non-NFC input |
| `effective_policy` | intersection of workspace and override, including a later workspace change |
| `ticket_event_sig`, `ws_event_sig` | signed bytes (with `contract`) and a P-256 signature; replay into another ticket or workspace refused; a genesis with all six checks of §5.11; a `drop:`-scoped certificate in `device.added` refused; a self-signed `device.added` refused; a stale `roster_v` refused |
| `chain` | 3 events: heads, `prev`, `host_sig`; a tampered line; a non-`cj` line |
| `generation` | the raise table of §5.7 row by row; a delayed approval after a change request refused; a reverted edit doesn't revive an approval; `gate.invalidated` raises nothing; an event matching several rows raises a gate once; done is sticky: watcher added on `done` → still `done`, workspace policy change → still `done`, push → `testing`, deleted ref → still `done`; reopen after `done` → the old verdict doesn't count |
| `status` | decisions in each state: requirements/plan approvals outside `done`/`closed`, verdicts and code decisions only in `testing`, a non-completing `pass` leaves the status |
| `revocation` | `device.revoked` by another member's device and by the host, authorised by the embedded revocation; payload `reason` ≠ `revocation.o.reason` refused; recovery `device.added` by the new device when none is left |
| `artifact_refs` | the reference regex, including inside code fences; an unknown reference refused; signer `refs` that differ from the regex refused |
| `source_list` | projection from `branch.pushed`, first observation, ref and identity changes |
| `question_id` | `qid` derivation and the question hash with it |
| `checkpoint` | the §2.4 shape; lower `seq` refused, equal `seq` with another head refused, a restore that re-appends revocations |
| `repo_identity` | https, ssh with and without a port, scp-like forms mapped to one canonical URL; an https URL with `user:token@` loses it; `file://` → `local:` |

## 12. Needs owner

These points change or sharpen the security model. Each was challenged by an adversarial Codex review and an
independent Opus security review; the final wording below is what the text above follows. They wait for the
owner's confirmation.

| # | Point | Final wording | Codex |
|---|---|---|---|
| N1 | Is every person event signed? | **Every event with a person actor is signed**, whatever its type (§5.2). Otherwise an agent holding the workspace key (P1, core §4) can forge "Severin commented …". | agreed with Codex |
| N2 | `presence` or `auth`; the P1 factor | `auth` replaces `presence` as **custody metadata**, covered by the signature but not a proof of the factor; no certificate field is invented (§5.3). P1 uses `passphrase` (D65), and `dk_sig` itself is passphrase-encrypted (O2). The weaker guarantees stay explicit: D49 (an unlocked iPhone signs) and D65 (a passphrase typed where an agent can read it is not protected). This weakens D41 for P1 and needs the owner's yes. | agreed with Codex, modified |
| N3 | Which key signs what | Person events: the **device key**, from a device whose certificate has `decide` (and `operate` for admin events); `drop:` certificates never sign; `device.added` needs an existing device of the same person. The person key signs device certificates, revocations and the workspace delegation (protocol §6, §7.1). `device.removed` (this workspace, protocol §6.3) is separate from `device.revoked` (global, protocol §6.2). | agreed with Codex, modified; scopes added with the Opus reviewer |
| N4 | `ORCH_GRANT` as a bearer secret | `gr_<ULID>.<secret>`, only `secret_hash` in the signed `grant.issued`. Printed once on the person's terminal, never written to a file by orch, redacted from orch's output and records, stripped from the environment of every process orch starts. Grants are always for their signer. **Stated limits:** in P1 an agent that can use the workspace key can forge agent events citing a valid grant, and can choose `ws_seq` and `at` (unsigned by the person) to place an event before a `grant.revoked` or `device.revoked`; replay can't catch either until P2 (key in the host) and P3 (relay checkpoints). It can also forge an invalid event to freeze person decisions until an owner signs `invalid.acknowledged` (R5); an accepted P1 denial of service. | agreed with Codex, modified |
| N5 | Invisible and bidi characters | Refuse bidi controls, C0/C1 controls (except LF, TAB) and code points unassigned in **Unicode 16.0** (pinned) in all ticket text; refuse an approval whose gated text has a bidi control; show other invisible characters as `⟨U+…⟩` (§11.3, §5.7). | agreed with Codex, Unicode pinned |
| N6 | External edits of `ticket.json`; questions | Every external change to `ticket.json` is reverted (`projection.repaired`); only `body.md` takes external edits. `question.asked` carries the **full question**, so `ticket.json` can be rebuilt from events. | agreed with Codex, modified |
| N7 | Verify gate, verdicts and stale decisions | A verdict **is** the verify decision (`pass`/`fail`). Every gate has a **generation**, raised in every status by the table in §5.7 (at most 1 per event); decisions sign `gate_gen` and `based_on`; a stale generation is refused and never counts; `gate.invalidated` raises nothing; later gates bind the earlier gates' generations and approvals (`prior`). | agreed with Codex, modified (generation barriers) |
| N8 | Host events | Not "only take away": `edit.external` **installs** prose. §5.12 lists the exact effect of each host event and states that a host event never approves, answers, grants, adds a member or creates a ticket. | Codex was right; changed |
| N9 | Unattended writes | Keep A3 (`ask`, `log`, `artifact add`) and change the harness plan's H2. Unattended writes only on tickets with `visibility: workspace`; quotas per session (30 events, 20 MiB per hour) and per workspace (120 events, 100 MiB per rolling hour); `question.asked` only for new ids; artifacts without `ac`/`task`, never evidence (O6). An unattended `artifact.added` can void a pending verify approval; accepted and visible. | agreed with Codex, modified; tightened with the Opus reviewer |
| N10 | The code gate (D59) | `not` always includes `assignees` and `independent` is always `true`, enforced by the host. Only a person's signature approves it; no `via` field in P1. In P1 turning it on moves no ticket back. | agreed with Codex |
| N11 | Addon writes | Addons hold no key; the host appends their events. `set_by` is a list of actor alternatives checked per write (`agent`, `addon`, human tokens = a signed person event). Only leaf paths can be set, so replacing `ticket.addons.<addon>` can't bypass `set_by`. Addon events never carry core authority. | agreed with Codex, modified |
| N12 | Independence of approvers | The policy option `independent`, **off by default** for `requirements`, `plan` and `verify`, so a sole owner can approve their own agents' work; always on for `code` (D59). See O4 for what `not: assignees` covers. | new from the Codex review |
| N13 | Trust root | The head of `workspace.created` is the genesis, checked in six ordered steps (§5.11); remembered by the host outside the workspace, in every workspace checkpoint, shown at pairing (the person's custody key file records it from P3 pairing on; `orch init` does not, see §10.7). Readers replay authorization, not only signatures. **P1 limit:** see O7. | new from the Codex review; tightened with the Opus reviewer |
| N14 | Rollback and trust in P1 | Equal-height divergent heads are refused and `restore` is defined, but **P1 can't detect a rollback when both the history and the local checkpoints are replaced**, and the genesis pin lives in files the same OS user owns (O7). Relay checkpoints (P3) and host-held keys (P2) fix it. | new from the Codex review |
| O1 | Where the factor is recorded | In the event's `auth` only, not in the device certificate. **D64's wording ("recorded in the person's device certificate") needs amending**; F1 doesn't edit D64. | agreed with Opus reviewer |
| O2 | Custody of `dk_sig` in P1 | `dk_sig` is held by the backend named in `auth`; in P1 it is passphrase-encrypted like the person key, decrypted for one signature on a TTY, never cached; a key that signs without its factor is `file`-tier and never signs person events (§5.3). | agreed with Opus reviewer |
| O3 | Done is sticky; compromised devices | A `done` ticket leaves `done` only by `ticket.reopened`, `branch.pushed` of a source ref (D58) or the D59 code-gate rule; bound edits are refused from every actor. A `device.revoked` with `compromised` voids that device's decisions on tickets not yet `done`; `done` tickets it approved show "approved by a revoked device", with no state change (§5.7). | agreed with Opus reviewer |
| O4 | Self-approval | With `independent` on, `not: assignees` also excludes the `for` person of agent events that touched a bound path in the gate's current generation. With it off (the single-owner default), a person may approve their own agent's work, and the doc says so (§5.7). | agreed with Opus reviewer |
| O5 | `restore` power | Owner only; never drops revocations (the host re-appends every PK-signed revocation it has seen); records the abandoned signed decisions in `abandoned_decisions` (§5.10). | agreed with Opus reviewer |
| O6 | Unattended evidence | Unattended artifacts carry no `ac`/`task` and are never evidence (§6). | agreed with Opus reviewer |
| O7 | P1 trust root | The genesis pin is in the host state dir (P1; the person's custody key file gets it at pairing), owned by the same OS user as the agents in P1, so an agent can replace them together; stated plainly next to N14. | agreed with Opus reviewer |
| R3 | Device recovery and who appends revocations | `device.revoked` may be appended by any member's device or by the host; its authority is the embedded PK-signed revocation (protocol §6.2). A device vouched for by the person key (with `decide`) may add itself when its person has no valid device left (the D50 recovery path); otherwise losing the owner's only device would leave the workspace without owner signatures. | agreed with Opus reviewer |
| R5 | Decision freeze after an invalid event | An owner signs `invalid.acknowledged {invalid_seq, invalid_head, reason?}` to lift the freeze; the event stays absent. An invalid event in the workspace log freezes all person decisions in the workspace until acknowledged. In P1 an agent with the workspace key can cause the freeze (N4). | agreed with Opus reviewer |

## 13. Decisions log (F1)

Sources: #335 Q1–Q7 (PR body), #335 CR (code review 6067420402), #335 SR (security review 6067462572), #336
A1–A20 (PR body), HO (dashboard handover, input only), D58–D60, the adversarial Codex review of F1 (rows 56–80) and the independent Opus security review with its re-review (rows 81–126).

| # | Gap (source) | Decision | Reason |
|---|---|---|---|
| 1 | Event names for implied events (#336 A1, HO) | The list in §5.4: 30 ticket-log and 14 workspace-log types (`projection.repaired` and `restore` appear in both logs). Renamed: `log` → `log.added`, `handoff` → `handoff.written`, `session.granted` → `grant.issued` (+ `grant.revoked`), `claim.expired` → `claim.released` (`expired`); from HO: `member.role_changed` → `role.changed`, `gate.policy_set` → `policy.changed`, `people.set` → `people.changed`, `labels.changed`/`section.edited` → `ticket.updated`, `lease.*` → `task.started` plus derived lapse, `task.run` → `task.done` receipt, `comment.added` → `log.added`, `workspace.grant_hours_set` → `settings.changed`, `device.paired`/`device.removed` → `device.added`/`device.removed`/`device.revoked`. `agent.refused` is not an event. HO's other workspace types wait for their phases. | One type per meaning; every core type is `<noun>.<verb>`, so it can't collide with an addon's `<name>.*`. |
| 2 | Which events carry `sig` (#336 A2) | Every person event, never another (§5.2). | §12 N1. |
| 3 | `presence` values (#336 A3) | Replaced by `auth` (D66) with six values (§11.4): custody metadata, signed, not a proof (§5.3). | §12 N2. |
| 4 | Actor of host events; `edit.external` "unattributed" vs required actor (#336 A4) | `actor` is always present; host events use `{"kind": "host"}`. One envelope schema. | No missing-field special case; "unattributed" means no person or agent credited. |
| 5 | Unattended marker (#336 A5) | `unattended: true` instead of `for`/`grant`, never both; three types only. | A3 as written (§12 N9). |
| 6 | Id formats (#336 A6) | §11.1. Person and device ids are the protocol ids with a prefix; `workspace_id` is 32 hex. | One id per thing across the format and the relay. |
| 7 | Bare hex vs `sha256:` (#335 Q7, #336 A7, SR 7) | `sha256:` + 64 hex everywhere, artifacts included. | One parser at every boundary; one encoding per binding. |
| 8 | Signature and key encoding (#336 A8) | b64u, no `p256:` prefix (§11.1). | Protocol §1.1 and §2.2; one suite per deployment (D46), and the suite is in the signed bytes. |
| 9 | `prev`/`based_on` of the first event; first `seq` (#336 A9) | `null` only on `seq` 1; `seq` starts at 1, no gaps. | A single, checkable chain start. |
| 10 | Undefined enums (#336 A10) | §11.4 (priority, size, approver tokens, release reasons, verdict `pass`/`fail`, field types). | Values the dashboard and v1 already use; `pass`/`fail` as in v1. |
| 11 | "spike / investigation" (#336 A11) | One type `spike`; its verification section is `findings`. | Two tokens for one meaning would split filters and rules. |
| 12 | Required `ticket.json` keys (#336 A12) | All 17 keys always present, with defaults (§3). | No absent-vs-default ambiguity in hashes, diffs or readers. |
| 13 | Body completeness (#336 A12) | Drafts may be incomplete; completeness is checked at approvals and submit (§4). "—" sections and unknown headings are refused on every write. | Tickets are written in steps; gates are where completeness matters. |
| 14 | Rules JSON Schema can't express (#336 A13) | Key order: the store. Current state: `handoff.written` ≤ 2 KB, otherwise not enforced. Cross-file references: the model. Addon fields: the manifest at runtime. | Each rule has one owner. |
| 15 | `links.external`, `due`, labels (#336 A14) | `https://` url strings; `due` is a date only; labels are `^[a-z0-9][a-z0-9._-]{0,31}$`. | Due dates are days; one form each. |
| 16 | `keys.jsonl` fields (#336 A15) | `{key, uid, at}`; a projection of `ticket.created` and the allocator; the next number is above anything seen (§2). | Never reused even if the file is edited. |
| 17 | Addon manifest shapes (#336 A16) | Minimal shapes in §8; `needs`, `cli`, `skills`, `agents_md` deferred to C9. | Enough for fields, sections and kinds in P1, nothing guessed. |
| 18 | Operation declaration (#336 A17) | Unchanged (core §3); the C5 schema choices stand. | Not part of the file format. |
| 19 | Checkpoint fields (#336 A18) | §5.10, with `suite`, `kind`, `workspace_id`, `at`, `genesis` (workspace kind) and a signature label. | Checkpoints can't be replayed across workspaces or kinds. |
| 20 | `wait` result fields (#336 A18) | §10.4 item 7, with `invalidated`. | D58 needs the agent to learn about a voided verdict. |
| 21 | Artifact kinds and names (#336 A19) | As implemented: core kinds, addon `addon` + `ref`, `feedback` only from a person, safe basenames (§6). | Already matched the doc. |
| 22 | Imported v1 history (#336 A20) | Settled by amendment C10 (§14): no new event type; the v1 ticket and its history travel as one hashed artifact. | Depends on what v1 history the importer keeps. |
| 23 | Trailing whitespace, final newline, blank lines (#335 Q1, SR) | Not normalised; only a section's leading and trailing LFs are not part of its text (§4). | Exact text is safer; whitespace changes void gates (fail closed). |
| 24 | Gate hash key set; `tasks`/`artifacts` placement (#335 Q2, CR 1, SR 1) | 15 top-level keys always present for every gate, with empty values where unused (§5.7). | Absent vs empty can't encode one approval twice; one recipe for every gate. |
| 25 | Gate hash domain label (#335 Q2, SR 2) | `orch/v2/gate\|`. | Every protocol hash is labelled; a section can no longer collide with a gate hash. |
| 26 | `section_hash` undefined (#335 Q3, CR 3, SR) | Defined: `orch/v2/section\|` over the section text, keyed by section id (§5.6). | Needed for `base_rev` and external edits; moving text between sections is a change. |
| 27 | Event chain hash (#335 Q4, SR) | `head` = `orch/v2/event\|` over `cj` of the full event; `host_sig` covers everything but itself (§5.5). | The head commits to the exact signed record, without a cycle. |
| 28 | Depth counting (#335 Q5, SR) | Root = level 1; 16 allowed, 17 refused. | Agreed by the security review; goes into the protocol vectors. |
| 29 | Lone CR (#335 Q6, SR) | CRLF and lone CR → LF, stated explicitly (§11.3). | A lone CR can hide text in a terminal. |
| 30 | Normalisation inside the gate payload (SR 3) | Every hashed string must already follow §11.3; hash functions refuse otherwise. | Fail closed; normalising happens once, at the store. |
| 31 | `dumps_general` in the public API (CR 2, SR 4) | Not a format matter: nothing in the format uses general RFC 8785; C1 makes it private. | Only `cj` exists in the contract. |
| 32 | Invisible and bidi characters (SR 5) | Refuse bidi and controls; show other invisibles (§11.3, §5.7). | §12 N5. |
| 33 | Lone surrogates in the text layer (SR 6) | Refused by the text rules; C1 maps the error to `HashError`. | Strings are Unicode scalar values (protocol §2.3). |
| 34 | Non-ASCII section names (SR 8) | Sections are keyed by ASCII ids; unknown headings are refused (§4). | Hash keys must be ASCII; custom headings would be unhashable. |
| 35 | NFC and the Unicode version (SR 9) | Refuse unassigned code points; Unicode pinned to 16.0 (row 68). | NFC is stable only for assigned code points. |
| 36 | What callers enforce (SR 10) | Roots are objects; sizes are checked before parsing (§11.2). | Written down once for every caller. |
| 37 | `policy_hash`/`people_hash` unchecked (CR 4) | Both are defined labelled hashes (§5.6) and validated as hashes. | A gate hash can't be built from arbitrary strings. |
| 38 | Missing signature labels (SR 2) | `orch/v2/sig/ticket-event\|`, `sig/ws-event\|`, `sig/host-event\|`, `sig/checkpoint\|`. | Each signer and object kind has its own domain. |
| 39 | Signing context form | `label \|\| cj({contract, suite, workspace_id, log, event})` instead of `…\|<workspace_id>\|<uid>` fields; `contract` is the signed-event contract version (1). | Protocol §2.4 style; no delimiter rules; the suite and contract version are bound. |
| 40 | `owner` in policies: workspace role or ticket owner? | `owner` is the workspace role; the ticket's owner is `ticket_owner`. | The config example meant the workspace owner. |
| 41 | Verify gate vs verdict | `verdict.given` is the verify decision; no `gate.approved` for `verify`. | §12 N7; one event per meaning. |
| 42 | D58 | The source list `[{repo identity, ref, sha}]` in the verify and code gate hash, read from git at prompt, append and landing; any ref-value change is a new head; host events `branch.pushed` and `gate.invalidated` (`new_commits`). | A ticket may link several repos; the host never trusts an agent's SHA. |
| 43 | D59 | Gate `code`, `applies` default `off`, `not` always includes `assignees`, `independent` always on, source list in its hash and on `gate.approved`, the verdict in its `prior`. | As decided; the same commit as the verdict. |
| 44 | D60 | Grant terms table (§10.1), `grant.issued`/`grant.revoked`, `settings.changed` for `grant_hours`. | As decided; members only `workable`. |
| 45 | Grant as a bearer token | `ORCH_GRANT` carries a secret; the event stores its hash; delivery, redaction and no inheritance to child processes (§10.1). | §12 N4. |
| 46 | Statuses | Six statuses and their derivation (§5.9); "waiting" is derived. | C4 needs them; the dashboard uses the same set. |
| 47 | Claim expiry | Derived from inactivity (`claim_ttl_min`, default 120) or the grant's end; the host records it lazily. | No timer process in P1. |
| 48 | Where ticket policy overrides live | A signed `policy.changed` in the ticket log, intersected with the workspace policy; never in `ticket.json`. | Policy is gate state (T14) and must be signed. |
| 49 | `ticket.updated` payload | `base_rev` map, new field values, new section hashes (§5.8). | The log shows what changed without copying prose. |
| 50 | Question hash | Protocol §13's label and shape, with `question_id` = the Q id; phone decisions are verified by the host and kept as `evidence` (§5.3). | One hash input for the CLI and the phone; the signed bytes differ and are not converted. |
| 51 | Change requests | Raise the generation of that gate and the later gates. | A changed requirement invalidates the plan built on it; generations make it permanent. |
| 52 | `list_seq` | Renamed `roster_v`: the member-list version at the event's position; a stale value is refused (row 86). | Signatures can't cite an old roster to act with an old role. |
| 53 | Where device certificates live | The workspace log (`device.added`); workspace removal and global revocation are separate events. | Every reader can verify human signatures offline. |
| 54 | Event line bytes | Each line is exactly `cj(event)`. | Hashing the parse and the line agree; a hand edit is caught. |
| 55 | D61, D62 | Not in the format; no `via` field, no mandate events. | Out of P1 (owner, 10 Oct). |
| 56 | `ticket.created` payload `type` collides with the envelope (Codex 1) | Renamed `ticket_type`; also in the gate hash `fields`. | One name, one meaning per object. |
| 57 | `auth` checked against a certificate field that doesn't exist (Codex 2) | `auth` is signed metadata only; no certificate field; signed-event contract versioned as `contract` (§5.3). | Don't invent protocol fields; protocol §6.1 is fixed. |
| 58 | Phone decisions vs `sig/ticket-event` (Codex 2) | Host verifies the protocol §13 decision and records it verbatim as `evidence`; the adapter is P3. The signed bytes differ. | A signature over one byte string can't become another. |
| 59 | "Older commit is not a new head" vs D58 vs D53 clean rebase (Codex 3) | Any ref-value change is a new head (D58). D53's clean rebase applies only to the landing worker's candidate, which records the approved `source_sha` and its `candidate_sha` and is re-checked (§5.7). | Approval binds one exact commit; landing is a derived, checked step. |
| 60 | A workspace-key holder forges surrounding state (Codex 4) | Readers replay authorization (actors, certificates, roles, grants, policies, generations, transitions); the genesis is pinned (§5.11). | Signatures alone don't make a forged `member.added` harmless; replay does. |
| 61 | Delayed approval appended after a later rejection (Codex 5) | Gate generations; decisions sign `gate_gen`; stale is refused and never counts (§5.7). | `seq`/`at` are host-chosen and outside the signature. |
| 62 | Checkpoints only refuse lower `seq` (Codex 6) | Equal `seq` with another head refused; diverged logs stop; restore defined with `abandoned`; P1 limit stated (§5.10). | Divergence at the same height is the common attack. |
| 63 | Gate hashes don't bind earlier gates; reverting content revives approvals (Codex 7) | `prior` binds earlier gates' generations and approval ids; voided approvals are retired by generation; `done` recomputed after every event (§5.7). | A plan approval is about a specific approved requirement. |
| 64 | Evidence bound only by name and digest (Codex 8) | Artifacts bound as {kind, digest, ac, task}; receipts with repo and commit; addon package digests; addon artifacts are never evidence. | What counts as evidence is what was approved. |
| 65 | `source_sha` from `links.branches` only, no repo identity or ref (Codex 9) | Source list with repo identity, ref and sha for every repo in `links.repos`; branches required before submit; repo paths in owner-signed `settings.repos`; checked at prompt, append and landing. | Two repos with the same branch name or a swapped remote can't alias. |
| 66 | Ticket override can become weaker after a workspace change (Codex 10) | The effective policy is a continuously recomputed intersection (§5.7). | Tightening holds by construction, not by a one-time check. |
| 67 | Host events "only take away" (Codex N8) | §5.12 lists each host event's exact effect. | `edit.external` installs prose; the claim was wrong. |
| 68 | Unicode version (Codex N5) | Pinned to Unicode 16.0, bundled tables where needed; a change is a new `hash_v`. | Two runtimes must agree on NFC and "unassigned". |
| 69 | Questions not rebuildable (Codex N6, 24) | `question.asked` carries the full question; `ticket.json` is rebuilt from events. | T14 and rebuildable projections. |
| 70 | Unattended scope (Codex N9) | Only `visibility: workspace` tickets; per-session quota; the verify side effect is accepted and visible. | Unattended means nobody vouches; keep it narrow. |
| 71 | Addon `set_by` bypasses (Codex N11) | Actor-based alternatives, leaf paths only, addon events carry no core authority. | Closes parent-object replacement. |
| 72 | Independence deadlocks a sole owner (Codex 22) | Policy option `independent`, default off except `code`. | A single owner must be able to approve agent work. |
| 73 | P1 scope too wide (Codex 23) | A5 narrowed: freeze the contracts with vectors (§11.5); defer addon execution and custom events, landing, relay checkpoints, other custody backends. | Freeze what is expensive to change; build the rest later. |
| 74 | Atomic file + event updates (Codex 24) | Pending files, event append, rename; crash recovery order (§5.5). | A crash must never look like an external edit or a host write. |
| 75 | Loose units (Codex 25) | Character limits in Unicode scalar values, sizes in UTF-8 bytes (exact numbers), grants with signed `issued_at` and `hours`. | Two implementations must count the same way. |
| 76 | Ordering the two logs | `ws_seq` on every ticket event. | Generations and replay need a total order across logs. |
| 77 | `done` and closed tickets taking edits | Agent edits of bound content are refused until `ticket.reopened`. | Avoids silent `done` → `testing` churn; reopening is explicit. |
| 78 | Policy changes and `done` tickets | A policy change doesn't move `done` or `closed` tickets (only D59's code-gate rule). | Old work isn't re-reviewed because a default changed. |
| 79 | Host events that void approvals | `gate.invalidated` gains `content_changed` and `policy_changed`. | Every void of counting approvals is visible in the log. |
| 80 | Device removal vs revocation (Codex N3) | `device.removed` (workspace) and `device.revoked` (global, PK-signed revocation, with a reason). | Protocol §6.2 and §6.3 are two different things. |
| 81 | Genesis unverifiable; owner never a member (Opus B1) | `workspace.created` carries `owner {person, name, pk_pub}`; six ordered checks; the genesis makes the owner a member, has `roster_v: 0`, and the version after it is 1. | Every reader can check the root of trust the same way. |
| 82 | Scoped or look-only devices sign person events and add themselves (Opus B2) | Person events need `decide` in `scopes_max`, admin events `operate`; `drop:` certificates refused; `device.added` by an existing device of the same person, first device via `member.added`/genesis. | Restores N1: only decision-capable devices speak for a person. |
| 83 | Custody of `dk_sig` unspecified (Opus B3) | Held by the `auth` backend; in P1 passphrase-encrypted, decrypted per signature on a TTY, never cached. | N1 rests on the device key being protected. |
| 84 | `done` both recomputed and stable (Opus B4) | Done is sticky; bound edits refused on `done`/`closed` from every actor; external edits of bound sections reverted; `people_hash` only covers roles a policy names. | Python, Swift and TS must compute the same state. |
| 85 | Gate hash and generations not a function of the logs (Opus B5) | Source list = projection of `branch.pushed` (incl. first observation, identity and ref changes); section hashes in `G`; regex for inline references recorded as `refs`; `binds` on `addon.granted`; a syntactic raise table and explicit `bound(g)`; `gate.invalidated` raises nothing; `ticket.links` bound to `verify` and `code`. | Any reader rebuilds `G` and generations from events alone. |
| 86 | `list_seq` backdating and name clash (Opus S1) | Renamed `roster_v`; must equal the version at the event's position (`members.stale`). | Authorization is evaluated at position; protocol §7.2 has its own `list_seq`. |
| 87 | `ws_seq` rules (Opus S2) | Non-decreasing, ≤ the workspace head; merged order defined; shared workspace lock; unsigned-`ws_seq` limit stated in N4. | A total, checkable order across logs. |
| 88 | Removal and role-change effects (Opus S3) | Evaluated at position; voids only gates not yet at `count`; `people` unchanged by removal; orphan questions answered by owners and maintainers. | No cascade through finished gates. |
| 89 | Replay failure semantics; forged derived fields (Opus S4) | Failed events are absent for state, keep their chain place, block new decisions; `voided`, `voided_gates`, `normalised` recomputed; `edit.external` is not followed by `gate.invalidated`. | One failure model for every implementation. |
| 90 | Unattended bypasses (Opus S5) | Workspace-wide rolling budget; unattended `question.asked` only new ids; unattended artifacts carry no `ac`/`task`. | Rotating session ids or re-asking can't be used to steer a ticket. |
| 91 | Status from-states (Opus S6) | A from→to table for every event (§5.9). | Replay can check transitions. |
| 92 | Torn writes vs external edits (Opus S7) | `.state/applied`; recovery only when the last event isn't applied; a torn write ends in `edit.external`. | An ordinary external edit isn't misreported, and `base_rev` keeps working. |
| 93 | Effective policy canonical form (Opus S8) | Every policy has all five keys; `applies` union rules; tokens, not persons, for "no eligible approver". | A partial override can't switch `code` on by accident. |
| 94 | Repo identity incomplete (Opus S9) | Raw `remote.origin.url`, port rules, scp-like forms, `local:` otherwise; duplicate paths and identities refused; P1 limit stated. | One canonical identity across runtimes. |
| 95 | Receipts (Opus S10) | `receipt.cmd` = `verify.cmd`; latest `task.done` of a done task; evidence only at the current source `sha`; prompt rechecks file digests. | Stale or substituted evidence doesn't count. |
| 96 | Restore drops revocations (Opus S11) | Revocations re-appended at once; `abandoned` gives up every checkpoint above `from_seq`; workspace restore raises every ticket's gates. | A rollback can't resurrect a revoked device. |
| 97 | Question id frozen wrongly; `evidence` half-defined (Opus S12) | Derived `qid` (32 hex, `orch/v2/question-id\|`) used in the question hash; `evidence` refused in P1, defined in P3 with a new contract. | Protocol §13 needs hex ids; no person event without `sig`. |
| 98 | `based_on` unchecked (Opus S13) | Must name an earlier event of the same log (`event.bad_base`); staleness via `gate_gen`, `hash`, `base_rev`. | A meaningless `based_on` can't be signed. |
| 99 | Genesis pin agent-writable (Opus S14) | Pinned in the host state dir (the custody key file only from pairing, P1 deviation in §10.7); human-only verbs refuse a different genesis; P1 limit in O7. | The human signer checks the root, not only the host. |
| 100 | Grant time checks (Opus S15) | `\|at − issued_at\| ≤ 300 s`, `expires_at == issued_at + 3600·hours`, role terms at position, all on replay. | Grants are checkable by any reader. |
| 101 | `not: assignees` bypass through claims (Opus S16) | Per O4: with `independent` on it also excludes the agents' `for` person; off by default, and self-approval is stated. | The owner's single-person default stays usable. |
| 102 | Three `v` fields (Opus note) | Envelope `v` (2), signed-context `contract` (1), checkpoint `o.v` (2, protocol §2.4). | Distinct names, no misreads. |
| 103 | Checkpoint shape (Opus note) | Protocol §2.4 `{"o", "sig"}` with kinds `ticket_checkpoint`, `workspace_checkpoint`. | Ready for the relay in P3. |
| 104 | Unbound fields (Opus note) | Listed under `bound(g)`; prompts don't present them as approved. | Honest prompts. |
| 105 | Claims across persons (Opus note) | Takeover by any covering grant and release by ticket owner/owners/maintainers, stated as accepted for P1. | Written down instead of implied. |
| 106 | Completeness on replay (Opus note) | An approval of an incomplete gate doesn't count on replay either. | Same result at append and replay. |
| 107 | `suite` wording (Opus note) | "the deployment suite (2, D46)". | Not a constant per format. |
| 108 | D64 wording (Opus O1) | Listed for amendment; not edited here. | D64 says the backend is in the certificate; it isn't. |
| 109 | Custom addon events in P1 | Refused until C9/P2 define them (A5). | Nothing unvalidated enters the logs. |
| 110 | Compromised devices (Opus O3) | Void decisions on tickets not yet `done`; flag on `done` tickets. | Sticky `done` with visible risk. |
| 111 | Raises suspended on `done` vs reopen/restore/push (re-review R1) | Generations are raised in every status; done is sticky is a separate status rule; reopen after `done` voids the old verdict. | One reading for every implementation. |
| 112 | Status table refusing ordinary decisions (re-review R2) | Events not in the table don't change status; requirements/plan approvals in any state but `done`/`closed`; verdicts and code decisions only in `testing`; a non-completing `pass` leaves the status. | A count-2 gate must be reachable. |
| 113 | Host can't re-append revocations; lockout after losing every device (re-review R3) | `device.revoked` P or H, authorised by the embedded revocation; recovery `device.added` by a PK-vouched new device when none is left. | Protocol §6.2: the PK signature is the authority; D50 recovery. |
| 114 | Several matching raise rows (re-review S-a) | An event raises a gate by at most 1. | +1 vs +2 would split `gate_gen`. |
| 115 | Branch deletion after landing (re-review S-b) | A missing ref is not a change; on `done` only a new `sha` on an existing ref counts; pending `branch.pushed` appended before a prompt. | Landed tickets don't bounce. |
| 116 | Credentials in the repo identity (re-review S-c) | Userinfo always removed. | No secrets in hashed, signed data. |
| 117 | Revocation reason mismatch (re-review S-d) | Payload `reason` must equal `revocation.o.reason`. | The compromised effects can't be dodged. |
| 118 | `.state/body` copy agent-writable (re-review S-e) | Used only when its section hashes match the log; otherwise `store.torn_write`. | A tampered copy can't become the reverted text. |
| 119 | Signer-computed `refs` (re-review S-f) | Refused when they differ from the regex (`body.bad_refs`). | Bindings come from the text, not the signer's claim. |
| 120 | Person releasing another's claim (re-review S-g) | Must name a live claim's session (`claim.not_live`). | No meaningless releases in the log. |
| 121 | Decision freeze lever (re-review S-h) | Owner-signed `invalid.acknowledged` lifts it; workspace-log case freezes workspace decisions; P1 DoS stated in N4. | A `restore` shouldn't be the only way out. |
| 122 | Stale text (re-review S-i) | §11.5 says `contract`; N7 updated; the `Q1` bullet removed; the `config.json` example policies have all five keys. | Consistency before freezing. |
| 123 | Addon actor in P1 | Refused entirely; §5.1 `type`, the addon actor row and `ticket.updated` D are marked "from P2". | Addons don't run in P1 (A5). |
| 124 | New event `invalid.acknowledged` | Both logs, owner only, needs `operate`; prefix `invalid` reserved. | S-h needs a signed, replayable way out. |
| 125 | Host appending `device.revoked` | Allowed only for a PK-signed revocation it holds (§5.12). | The host never makes up a revocation. |
| 126 | Vectors | `generation` (multi-row, deleted ref, reopen), `status`, `revocation`, token URL, bad `refs` added to §11.5. | The fixes are pinned before the freeze. |
| 127 | Text outputs of reads vs the `ok` line (#346) | Every success template starts with `ok`; reads put their body in result lines; the §10.5 `status` sample is fixed (§10.4 item 2). | The `operation` schema pattern and §10.4 already required `ok`. |
| 128 | Text error format and streams (#346) | `err <code> <message> · retry:<bool> · next: <hint> · fix: <cmd>`, fixed separators, `·` escaped; text on stderr, JSON envelope on stdout; `ok:false` and a non-zero exit together (§10.4 item 4). | One parseable line; one JSON document on one stream. |
| 129 | Retry dedup key (#346) | Session, grant id or `none`, attended mode, operation, normalised args, head `seq` for ticket writes; 15 min; writes only; not with `--dry-run`; grant re-checked; redacted before caching (§10.4 item 9). | A duplicate can't outlive a grant or leak a secret, and a changed ticket is a new write. |
| 130 | Stop rule details (#346) | Advisory; same refusal three times in 15 min returns `stop`; `human_only` ignores args; only a successful non-dry-run write resets; usage, retryable and internal errors (incl. `not_implemented`, exit 1) don't count; no session means no stop rule and no dedup; malformed `ORCH_SESSION` is invalid.input (§10.4 items 5, 6). | "Three in a row" needed a definition of same, window and reset. |
| 131 | Grant secrets in arguments and environment (#346) | grant.secret_in_args (exit 2) before parsing; redaction before truncation everywhere; handlers get no `ORCH_GRANT`; a malformed `ORCH_GRANT` is refused for every write, reads ignore it (§10.4 item 12). | N4: a secret must not reach logs, output or child processes. |
| 132 | Echoed text and `fix.argv` (#346) | Echoed agent text escaped (`⟨U+…⟩`, C0/C1/ESC); JSON escapes C1, bidi and line separators; `fix.argv` starts with `orch` with clean elements, else `internal` (§10.4 item 11). | Terminal escape and bidi tricks in error output. |
| 133 | Argument parsing rules (#346) | `--json`, `--help`, `-h` only before `--` and never as an option value; a scalar flag twice is a usage error; `ask --options` splits on commas with token-pattern options; `set` keys are title, priority, size, labels, due, links, parent, blocked_by (§10.3, §10.4 item 13). | Free text with commas or flag-like values must not change the command. |
| 134 | Last owner, viewers, duplicate approvals (#347) | The last owner can't be removed or demoted (`members.last_owner`); a viewer writes only answers addressed to them and device events; a duplicate approval by one person counts once (§5.9, §5.11). | A workspace keeps an owner; counts are by person. |
| 135 | Addon binds and repo links (#347) | `binds.fields` keys belong to the addon in the same event; `links.repos` must be in `settings.repos`, and a stale repo blocks only edits touching links (§5.11). | One addon can't bind another's fields; an old repo doesn't freeze unrelated edits. |
| 136 | Initial policies and merged order (#347) | Initial workspace policies are the §2 config defaults; merged order `(ws_seq, at, uid, seq)`; an append sorting before the last is refused (`chain.bad_ws_seq`) (§5.11). | A total order every reader computes the same way. |
| 137 | Cross-ticket references (#347) | `parent`, `blocked_by`, `duplicate_of` must point to a ticket created earlier in merged order (§5.11). | No forward or dangling references on replay. |
| 138 | Replay pin and Verifier interface (#347) | `replay` requires the expected workspace id and refuses a genesis that differs from the pin (`trust.genesis_mismatch`); `verify_person(event, context)`, `verify_host(event, *, log, wsk_pub, workspace_id)` (never from the event), `verify_embedded(event, *, pk_pub, device_cert=None)` (cert required for `device.revoked`) (§5.11). | The caller supplies the trust anchors; the event can't. |
Open after F1 (not settled here):

- The P3 envelope for phone decisions (`evidence`); the `qid` mapping itself is settled (§5.6).
- orch-relay `vectors_v2.json` needs the vectors of §11.5. That is an orch-relay change.
- Bundled Unicode 16.0 tables for Python 3.11–3.13 (C1).
- Addon event payloads and the `needs` language (C9). (Imported v1 history is settled by §14.)
- D63–D66 are still drafts; `auth` (N2) depends on them, and D64's wording needs amending (O1).

## 14. Amendment C10: importing v1, `doctor` and `check`

Draft of 11 Oct 2026, to be challenged by a second reviewer before merge. It settles row 22 of the decisions log.

### 14.1 What the import does, and does not

`orch import v1 PATH` reads a v1 workspace (its `orchestrator/` folder) **read-only** and writes, for each v1 ticket, the
signed events of a v2 ticket that keeps its key. It adds **no event type**. A new type would be code every reader must
understand forever, and the question "does it count for gates?" would be a rule to get right; the existing person events
already have exactly the meaning wanted (signed by the importer, counting for nothing they did not already count for).

| v1 | v2 | Event |
|---|---|---|
| `id` `DEMO-0043` | the same key (the workspace prefix must equal v1's; a key already taken skips that ticket) | `ticket.created` |
| `title`, `type` (`investigation` is `spike`) | `title`, `ticket_type` | `ticket.created` |
| `priority` (`normal` is `medium`), `size` (`xl` does not exist in v1), `due`, `labels` | the same fields; labels are made into v2 tokens, plus the label `imported-v1` | `ticket.updated` |
| `parent`, `blocked_by` | kept only for tickets that exist in v2 by then (parents and blockers are created first) | `ticket.updated` |
| `repos`, `branches`, `prs` | only for repositories in `settings.repos`; `external` urls only when `https` | `ticket.updated` |
| Ask, Context | `context` (the Ask first, labelled) | `ticket.updated` |
| Summary, Requirements, Out of scope, Plan, Verification, Findings | the same section when the ticket's type has it; **otherwise** (and for the v1 Log and sections v1 does not know) a labelled block `**v1 <name>:**` at the end of `context`. A section over 64 KiB is cut with a note (the whole text is in the history) | `ticket.updated` |
| Acceptance criteria | `acceptance` `AC1..` in order (the checkbox state is dropped) | `ticket.updated` |
| Tasks | `tasks` `T<n>` with text and `proves` from `ref: ac:N`; `verify` is `null`, state is dropped | `ticket.updated` |
| Current state | a note (where it came from, the v1 status, which sections were kept in Context, the v1 task states) and then v1's text, at most 2 048 bytes (cut with a note) | `ticket.updated` |
| open questions | `question.asked` (`to: ticket_owner`); answered ones stay in the history | `question.asked` |
| artifacts with a file | `artifact.added`, kind mapped (`receipt` becomes `log`, `feedback` becomes `other`), **no `ac`, no `task`**; the v1 links are in the `label` | `artifact.added` |
| artifacts that are links or `static/` files, answered questions, follow-ups, sprints, the ledger | only in the history artifact | |
| the whole v1 ticket file and its events (`.state/events.jsonl` lines of the ticket) | the history artifact `v1-import.json` (file kind `other`) | `artifact.added` |
| status: **the folder** the file is in (as in v1; the frontmatter's own `status` is only recorded in the history). `backlog` | `backlog` | `status.changed` |
| status `open`, `in-progress`, `waiting`, `testing` | `open`: a claim needs a grant and a session that no longer exist | |
| status `done` | `closed` (`resolution`: completed becomes `other`, wont-do `wont_do`, superseded `obsolete`, duplicate `duplicate` with `duplicate_of` when that ticket was imported), with the text "Imported from v1: done" | `ticket.closed` |
| the end of a ticket | `log.added` "import.v1: complete <digest of the history artifact>" | `log.added` |

### 14.2 Decisions and why

1. **Who signs.** The person at the terminal, a **human-only** operation: the same device key and the same presence as
   `approve`. The importer is the owner of every ticket. An agent gets `human_only`. Before the passphrase, the
   terminal shows what will be signed (counts, v1 status, the first tickets, a digest of the whole plan, what is *not*
   carried) and asks to type `IMPORT <n>`.
2. **One passphrase for the batch.** 8 tickets are about 40 signatures, a real workspace thousands. A prompt per
   signature (§5.3) would make the import unusable, so `import v1` uses one unlock for the run
   (`PassphraseBackend.unlocked`): the passphrase prompt (its own layout, "for a batch of signatures") carries the plan
   digest (`sha256` of the step contents under the label `orch/v2/import-plan|`; seq, prev, `at` and `base_rev` are
   assigned when each event is appended), the unlocked key lives in the process until the command ends, then it is
   zeroised. It is **bound**: the key signs only the ticket-event label, and only through a signer that accepts a
   person event of a type `import.v1` emits, in a ticket log of the reviewed plan, by the importer on its device, equal
   to a planned step. For the length of the window core dumps are off and a debugger cannot attach (`RLIMIT_CORE` 0,
   `PT_DENY_ATTACH` on macOS, `PR_SET_DUMPABLE` on Linux). A test checks that only `ops/import_run.py` calls it; a
   backend without such a mode cannot import. Events signed this way carry `auth: passphrase`: that covers "entered
   for this signature, or once for a reviewed `import v1` batch". This is the only place where §5.3 "decrypted only for
   one signature" is relaxed, and only after the person has seen the whole batch. The plan is built before the prompt and signed as
   built: files changed in v1 meanwhile change nothing (an artifact whose bytes differ from the planned digest makes
   that ticket fail, not import).
3. **No v1 decision becomes a v2 decision.** Approvals, verdicts, task states, claims, evidence links (`ac`, `task`),
   receipts and the ledger are v1 facts, signed (if at all) with v1 keys by a v1 CLI that an agent could have
   fooled by editing files. v2 gates bind a v2 hash and a device signature at a generation. So every gate starts
   pending, a v1 `done` ticket is `closed` (reopen it and re-verify), and imported artifacts are never evidence.
   Cost: one approval per gate again. Benefit: nothing an agent wrote into v1 can arrive as an approval.
4. **No command is imported.** `verify` is `null`: `task done --run` would run it under the owner's signature.
5. **v1 data is untrusted.** Frontmatter is parsed by a restricted YAML reader (no anchors, tags or block scalars);
   every read walks the path with `O_NOFOLLOW` per component and caps sizes (ticket 1 MiB, artifact 64 MiB, event line
   1 MiB), so a symlink or a device file is refused, not followed. Text goes through §11.3: controls, bidi controls,
   unassigned code points and lone surrogates are replaced with U+FFFD and counted (the original stays in the history
   artifact); grant-shaped secrets are redacted. A hard link to a file outside is a regular file to the kernel and
   cannot be told apart: the person sees what is imported.
6. **All or nothing per ticket, resumable.** The whole plan is judged with `orch.model.preview`, each step on top of the
   ones before it, before anything is signed; a ticket the model refuses is skipped with the reason and does not
   count. The order of a ticket's events is fixed: creation, history artifact, files, one `ticket.updated`, questions,
   status or close, and the closing `log.added`. A ticket is **complete** only when that last event exists.
7. **Idempotent.** The ticket uid is derived from the v1 workspace (a hash of customer and prefix) and the key, with the
   v1 creation time as the ULID time, so the same v1 ticket always has the same uid. A second run: complete tickets
   are left alone (even if v1 or v2 changed since: nothing is ever updated or overwritten), an interrupted ticket
   is finished from what its log already holds, and a ticket whose history artifact has another digest than
   today's v1 file is reported as "v1 changed since" and not touched. A key taken by another v2 ticket skips the
   ticket.
8. **Refused:** a different prefix, and a v1 `id.pad` below 4 (v2 keys have at least four digits: the whole run, before
   any signature); a symlinked `orchestrator/` below the named folder; a path without `orchestrator/config.json` (schema 1);
   anything that is not a regular file below the v1 folder; a ticket the model refuses; a missing device key or
   terminal (§10.8).
9. **The emits table** (`model/emits.py`) lists the event types of `import.v1`. The entry changes with this amendment
   (no v2 release exists yet, so no log written under the old entry exists). Because a table entry could make a grant
   that names a person's operation cover an agent's events, the rule of §10.1 is now a **model** rule: a grant verb that
   names an operation with `who: human` (`model.emits.HUMAN_ONLY`, equal to the registry's human operations, tested)
   grants nothing, on replay as well as at issue.
10. **Also recorded.** The importer refuses to continue a ticket that has the derived uid but was not created by this
    importer's person. Marker text has grant-shaped secrets redacted. A hard-linked artifact (`st_nlink` above 1) is
    listed as kept-with-a-warning. The review lists skipped and already-imported tickets and what each ticket keeps.
    **Follow-ups, not done:** keep one dir fd for the whole run; progress output while signing; the C8 instructions
    could say the text of an `imported-v1` ticket is v1 data until a person approves its gates.

### 14.3 `doctor`

`orch doctor` replays everything with the real verifier through a **read-only** store (no key to append with, no crash
recovery, no index rebuild, no pin written) and reports `where: code: what` with the log, the `seq` and the cause the
replay gives:

| Check | Code |
|---|---|
| a line that is not the canonical bytes, a bad `prev`, `seq`, `ws_seq`, `host_sig`, or a person signature | `chain.broken` (log, `seq`, cause) |
| a log shorter than, or with another head than, a signed checkpoint | `chain.diverged` |
| a checkpoint that is unreadable or not signed by the host key | `checkpoint.bad` |
| a missing workspace or ticket checkpoint: an **error** where this machine holds the workspace key (it writes one after every append, so someone deleted it), a warning on a clone | `checkpoint.missing` |
| a ticket folder with no event log; a file in `artifacts/` the log does not name | `ticket.nolog`, `artifact.unlisted` (warning) |
| the genesis pin missing (warning), or different | `pin.missing`, `trust.genesis_mismatch` |
| a device revocation the host noted that the log does not hold | `revocation.missing` |
| an event that fails authorization and is not acknowledged | `auth.invalid_event` |
| `ticket.json`, `body.md`, `config.json`, `keys.jsonl` or an artifact file that differs from the log | `projection.*`, `artifact.mismatch`, `artifact.missing` |
| the derived index missing or stale | `index.stale` (warning) |
| a repository in `settings.repos` that cannot be read | `repo.unobservable` (warning) |
| key folders of an init that died (a marker of a process that is gone; what `--repair` removes) | `keys.orphan` (warning) |
| installed instructions that are stale (C8) | `instructions.stale` (warning) |

Exit 0 when there is no error (warnings are listed), 5 otherwise; a failing run's first line is `doctor: N errors, M warnings`, never `ok`. **Limit:** a whole workspace forged under a *new* id has no pin on this machine: it is trust on first use, so doctor reports `pin.missing` (a warning). Only a pin made when the real workspace was created, or a relay checkpoint (P3), catches it. `--repair` does only these, through the store: answer
external edits with `scan` (the host events of §5.8), rebuild the index, remove dead init keys. It never touches a
chain, a signature or a checkpoint: an owner-signed `restore` is the only repair for those.

### 14.4 `check`

`orch check` is the fast subset (chain, projections, checkpoints missing where the host key is, store reports, instructions) and exits 5 on any finding, with the first line `check: N problems`; it does not heal.
`orch check --staged` is the commit check for a git pre-commit hook (`orch check --staged || exit 1`): it also refuses
staged files under `.state/`, key files, files holding a grant secret, a symlink or submodule where the workspace keeps a file, a deleted log or projection, a staged `ticket.json`, `body.md`, artifact,
`config.json` or `keys.jsonl` that is not what the log says, and a staged log that is not a prefix of the verified one
(`commit.state`, `commit.secret`, `commit.edited`, `commit.forged`, `commit.link`, `commit.deleted`). The hook installer stays with C28 (P2).
