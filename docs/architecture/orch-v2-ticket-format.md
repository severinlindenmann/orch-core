# orch v2: ticket and artifact format

Status: **decided by the owner on 8 Oct 2026**, after three independent expert reviews (data format, security and
multi-user, agent ergonomics). Visual examples at four stages:
[orch v2 Ticket Examples](https://claude.ai/artifact/6vWgYR8kTxgnPCysfPqpnq).

**Amendment F1 (draft, 10 Oct 2026):** settles every gap found while building C1 (issue #338, PR #335 and #336
reviews), adds D58–D60 (verdict binds to the commit, the optional code gate, grant terms) and aligns with
orch-relay `docs/protocol-v2.md`. D61 (factory auto-approval) and D62 (mandates) are not part of the format in P1.
What changed and why is in the decisions log (§13). Points that change the security model are listed in §12 and
need the owner's confirmation. Revised the same day after an adversarial Codex review (§13, rows 56–80).

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
    "requirements": {"approvers": ["owner"], "count": 1},
    "plan": {"approvers": ["owner"], "count": 1},
    "verify": {"approvers": ["reviewers"], "count": 1, "not": ["assignees"]},
    "code": {"approvers": ["owner", "maintainer"], "count": 1, "not": ["assignees"], "applies": "off"}
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
v1's guards against forged headings.

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
| `v` | int | always `2` |
| `id` | ULID | chosen by whoever builds the event (the signer for person events). A repeated `id` in the same log is refused. |
| `seq` | int | `1` for the first event of a log, then +1, no gaps. Order is by `seq` only. |
| `at` | timestamp | the host's clock when it appends |
| `type` | string | a type from §5.4, or `<addon>.<verb>` |
| `actor` | object | always present (§5.2) |
| `based_on` | hash or null | the log head the actor saw. `null` only on `seq` 1. |
| `prev` | hash or null | the log head when the host appended. `null` only on `seq` 1. |
| `hash_v` | int | always `1` in this version (the recipe of §5.5 and §5.6) |
| `ws_seq` | int ≥ 1 | ticket-log events only: the workspace-log `seq` when the host appended. It orders the two logs against each other. |
| `list_seq` | int ≥ 0 | person events only: the member-list version the signer saw (§5.4.2) |
| `auth` | token | person events only (§5.3) |
| `sig` | b64u | person events only (§5.3) |
| `host_sig` | b64u | every event (§5.5) |

Then the payload fields of the type (§5.4). Each type has an exact field set: a missing required field or any extra
field is refused. Payload fields never reuse an envelope name.

```json
{"v":2,"id":"01J9ZP…","seq":5,"at":"2026-10-09T09:10:11Z","type":"gate.approved","gate":"requirements","hash":"sha256:fa37…","policy_hash":"sha256:31c2…","gate_gen":2,"hash_v":1,"ws_seq":31,"list_seq":7,"actor":{"kind":"person","id":"p_sev","device":"d_mac"},"auth":"passphrase","based_on":"sha256:aa91…","prev":"sha256:aa91…","sig":"Mz4…","host_sig":"q8T…"}
{"v":2,"id":"01J9ZQ…","seq":9,"at":"2026-10-09T10:40:22Z","type":"task.done","task":"T2","receipt":{"cmd":"dbt seed --select tariffs","exit":0,"ms":38200,"repo":"acme-energy-dbt","commit":"b7e1f02c…"},"actor":{"kind":"agent","id":"claude-code","session":"s_01J9…","for":"p_sev","grant":"gr_01J9…"},"based_on":"sha256:51e0…","prev":"sha256:51e0…","hash_v":1,"ws_seq":31,"host_sig":"Zr1…"}
{"v":2,"id":"01J9ZR…","seq":10,"at":"2026-10-09T10:41:03Z","type":"artifact.added","name":"seeds-in-warehouse.png","kind":"screenshot","sha256":"sha256:3f9a…","bytes":84213,"ac":"AC1","actor":{"kind":"agent","id":"claude-code","session":"s_01J9…","for":"p_sev","grant":"gr_01J9…"},"based_on":"sha256:98ac…","prev":"sha256:98ac…","hash_v":1,"ws_seq":31,"host_sig":"b2W…"}
```

### 5.2 Actors

| Kind | Shape | Signs | May append |
|---|---|---|---|
| person | `{kind: "person", id, device}` | always: `sig` and `auth` | types marked P or A in §5.4 |
| agent | `{kind: "agent", id, session, for, grant}` | never | types marked A |
| agent, unattended | `{kind: "agent", id, session, unattended: true}` | never | only `question.asked`, `log.added` and `artifact.added` (not `feedback`), on tickets with `visibility: workspace`, within the quota below (A3, §12 N9) |
| addon | `{kind: "addon", id: <addon name>, grant: <id of its addon.granted event>}` | never | its own `<addon>.*` events, and `ticket.updated` of leaf paths `ticket.addons.<its name>.<field>` whose `set_by` includes `addon` (§12 N11) |
| host | `{kind: "host"}` | never (only `host_sig`) | types marked H, and nothing else |

- **Every event has an actor.** Host events (`edit.external`, `projection.repaired`, `gate.invalidated`, …) carry
  `{"kind": "host"}`. "Unattributed" (T9) means no person or agent is credited; it does not mean the field is
  missing. There is one envelope schema, not two.
- **A person event is always signed**, whatever its type (§12 N1). An agent can use the workspace key in P1 (core
  §4), so it could forge any unsigned event; an unsigned person actor would let it put words into a person's mouth.
- `sig`, `auth` and `list_seq` appear on person events and on no others.
- Types marked H are refused from every other actor, and the host never appends a P or A type in its own name.
- An agent's `for` must be the person who issued `grant`. The grant must be valid at `at`: not expired, not revoked,
  its scope covers the ticket and its verbs cover the operation (§10.1 A3). `unattended` and `for`/`grant` never
  appear together.
- **Unattended quota:** per session, at most 30 unattended events and 20 MiB of artifacts per hour; more is
  refused with `quota.unattended`. An unattended `artifact.added` changes the verify gate's input, so it can void a
  pending verify approval; this is accepted and visible in the log.
- **Addon events never carry core authority.** `<addon>.*` events change no core state (status, people, gates,
  claims, evidence, grants); only the addon's own view reads them.

### 5.3 Human signatures and `auth`

- A person event is signed with the **device signing key** (`dk_sig`) of `actor.device`. The device's certificate
  (orch-relay protocol §6.1, signed by the person key) is in the workspace log (`device.added`), so every reader can
  verify it. The person key signs only device certificates, revocations and the workspace delegation (protocol
  §6, §7.1).
- **Signed bytes (signed-event contract v1):** `"orch/v2/sig/ticket-event|" || cj({"v": 1, "suite": 2,
  "workspace_id": W, "log": <uid>, "event": E})` for the ticket log, and `"orch/v2/sig/ws-event|" || cj({"v": 1,
  "suite": 2, "workspace_id": W, "log": "workspace", "event": E})` for the workspace log. `E` is the event without
  `seq`, `at`, `prev`, `ws_seq`, `sig` and `host_sig`. The signature covers `id`, `type`, `actor`, `auth`,
  `based_on`, `list_seq`, `hash_v`, `gate_gen` (on decisions) and the payload. It can't be replayed into another
  ticket, log or workspace, and the generation (§5.7) stops a delayed decision from landing after a later one.
  A change to these bytes is a new contract version `v`, never a silent edit.
- **`auth` is custody metadata, not proof.** It names the backend the signer's client used (D64, D66):
  `passphrase`, `secure-enclave` (Mac, user presence per signature, D41), `secure-enclave-unlocked` (iPhone, D49),
  `webauthn`, `tpm`, `windows-hello`. It is covered by the signature, so nobody else can change it, but the format
  can't prove the factor: the device certificate (protocol §6.1) has no backend field, and F1 adds none. A
  workspace policy may later refuse named factors for named verbs (D66); the default accepts any. The weaker
  guarantees stay as stated where they were decided: `secure-enclave-unlocked` signs whenever the phone is unlocked
  (D49), and `passphrase` does not protect a passphrase typed into a terminal an agent can read (D65,
  orch-v2-portable-custody.md §3).
- `presence` (in the 8 Oct draft) is replaced by `auth`.
- **Phone decisions (P3/P4).** A phone answers a question with protocol §13's `decision`, signed over
  `"orch/v2/sig/decision|" || cj({workspace_id, question_id, content_hash, decision_id, answer})`. **These signed
  bytes differ from `sig/ticket-event|`**, and one can't be turned into the other. The host verifies the decision
  against the device certificate and the current question, then appends `question.answered` with the person actor
  and the verbatim protocol decision as `evidence` in place of `sig`. The adapter (mapping `Q` ids to the protocol's
  `question_id`, and the b64u vs `sha256:` hash forms) is defined in P3. Until then a person event carries `sig`.

### 5.4 Event types

Actor column: **P** person only (signed); **A** agent with a grant, or a person (signed); **U** also an unattended
agent; **D** an addon; **H** the host only. Types marked D58–D60 are new with those decisions. Field types: §11.1.
`?` marks an optional field (absent, never `null`, unless the type says "or null").

#### 5.4.1 Ticket log

| Type | Actor | Payload | Notes |
|---|---|---|---|
| `ticket.created` | A | `key`: key; `ticket_type`: ticket type; `title`: str; `owner`: person id | Always `seq` 1. `owner` is the person actor, or the agent's `for`. Status `open`. (`ticket_type`, because `type` is the envelope's.) |
| `ticket.updated` | A, D | `base_rev`: {path: hash}; `set?`: {path: value}; `sections?`: {section id: hash or null} | At least one of `set`, `sections`. Leaf paths only (§5.8). `null` removes a section. Refused on `done` and `closed` tickets for bound paths (§5.9). |
| `status.changed` | A | `from`: status; `to`: `backlog` or `open`; `reason?`: str | Manual moves only, between `open` and `backlog` (§5.9). |
| `ticket.submitted` | A | (none) | `in_progress` → `testing` when every AC has evidence. |
| `ticket.closed` | P | `resolution`: `wont_do`, `duplicate`, `obsolete` or `other`; `duplicate_of?`: key; `text?`: str | → `closed`. `duplicate_of` only with `duplicate`. |
| `ticket.reopened` | P | `text?`: str | `done` or `closed` → `open`. Raises every gate's generation. |
| `visibility.changed` | P | `visibility`: as in `ticket.json` | Also rewrites `ticket.json`. |
| `people.changed` | P | `role`: `owner`, `assignees`, `reviewers` or `watchers`; `add`: [person id]; `remove`: [person id] | For `owner`, `add` has exactly one id and the old owner is removed. |
| `policy.changed` | P | `gates`: {gate: policy} | In the ticket log: an override, intersected with the workspace policy (§5.7). |
| `claim.taken` | A | `takeover?`: {`from_session`: session id, `reason`: str} | Agents only. One claim per ticket. → `in_progress`. |
| `claim.released` | A, H | `session`: session id; `reason`: claim release reason (§11.4) | An agent releases only its own claim. |
| `task.started` | A | `task`: task id | The lease (A4). Agents only. |
| `task.done` | A | `task`; `receipt?`: {`cmd`: str, `exit`: int, `ms`: int, `repo`: repo name or null, `commit`: git commit id or null}; `log?`: artifact name; `text?`: str | With `--run`, `exit` must be 0, otherwise nothing is appended. `commit` is the head of `repo`, read by the CLI from git (the receipt's source revision). |
| `task.skipped` | A | `task`; `reason`: str | |
| `task.blocked` | A | `task`; `reason`: str | |
| `task.reopened` | A | `task`; `reason?`: str | |
| `handoff.written` | A | `text`: str, at most 2 048 bytes | Rewrites Current state. `orch handoff` also releases the claim (`claim.released`, `handoff`). |
| `log.added` | A, U | `text`: str, at most 4 096 bytes | Agent notes and human comments. |
| `question.asked` | A, U | `question`: the full question object as in `ticket.json` (`id`, `to`, `text`, `why?`, `options?`, `recommended?`, `blocking`); `hash`: question hash | The last `question.asked` for an id defines it; `ticket.json` `questions` is its projection. Re-asking changes the hash, so answers to the old text no longer count. |
| `question.answered` | P | `question`: question id; `hash`; `option?`: option key; `text?`: str; `evidence?`: protocol decision (P3, §5.3) | At least one of `option`, `text`. Only the `to` person or a holder of the `to` role, or an owner or maintainer. |
| `gate.approved` | P | `gate`: `requirements`, `plan` or `code`; `gate_gen`: int; `hash`: gate hash; `policy_hash`; `source_sha?`: source list | `source_sha` only and always on `code` (D59). |
| `gate.changes_requested` | P | `gate`: `requirements`, `plan` or `code`; `gate_gen`; `hash`; `policy_hash`; `text`: str | Raises the generation of this gate and the later ones. |
| `verdict.given` | P | `outcome`: `pass` or `fail`; `gate_gen`; `hash`: verify gate hash; `policy_hash`; `source_sha`: source list; `text?`: str | The verify gate's decision. `text` is required on `fail`; a `fail` raises the generation of `verify` and `code`. `source_sha` is D58. |
| `gate.invalidated` | H | `gate`: gate; `cause`: `new_commits`, `content_changed`, `policy_changed` or `conflict_resolved`; `source_sha?`: source list; `voided`: [event id] | **D58.** Appended when counting approvals are voided. `source_sha` is the new head (with `new_commits`). `conflict_resolved` is D53 (P2). |
| `branch.pushed` | H | `repo`: repo name; `repo_id`: repo identity; `ref`: str; `sha`: git commit id; `before`: git commit id or null | **D58.** The host saw the ref's value change in git (any change: new commit, rebase, force-push, reset). Never taken from an agent. |
| `artifact.added` | A, U | file: `name`, `kind`, `sha256`: hash, `bytes`: int; or addon: `name`, `kind`, `addon`, `ref`: str; both: `task?`, `ac?`, `label?`: str | `kind` `feedback` only from a person; unattended never `feedback`. An addon artifact is never evidence. |
| `artifact.replaced` | A | as `artifact.added`, plus `replaces`: hash | Same `name`; `replaces` is the old digest. |
| `edit.external` | H | `sections`: {section id: hash or null}; `voided_gates`: [gate]; `normalised`: bool | A `body.md` change the host didn't write (§5.8). |
| `projection.repaired` | H | `path`: str; `cause`: `external_edit`, `projection_mismatch` or `keys_mismatch`; `fields?`: [path] | Both logs. |
| `restore` | P | `from_seq`: int; `head`: hash; `abandoned`: {`seq`: int, `head`: hash} or null; `reason`: str | Both logs. Owner only (§5.10). Raises every gate's generation. |

#### 5.4.2 Workspace log

| Type | Actor | Payload | Notes |
|---|---|---|---|
| `workspace.created` | P | `workspace_id`; `prefix`; `host_id`; `wsk_pub`: b64u; `delegation`: signed object; `device_cert`: signed object | Always `seq` 1, by the owner: the **genesis** (§5.11). `delegation` is protocol §7.1's one-time delegation, signed by the owner's person key; `device_cert` is the signer's certificate, so the first signature can be checked. |
| `member.added` | P | `person`: person id; `name`: str; `role`: member role; `pk_pub`: b64u | |
| `member.removed` | P | `person` | Voids unused approvals, releases claims (`member_removed`), re-routes questions to the ticket owner. |
| `role.changed` | P | `person`; `role`: member role | |
| `device.added` | P | `device`: device id; `cert`: signed object | Protocol §6.1 certificate, verbatim. Signed by the new device itself (proof of possession). |
| `device.removed` | P | `device`; `reason?`: str | Removes the device from **this** workspace only (protocol §6.3). By the device's person or an owner. |
| `device.revoked` | P | `device`; `revocation`: signed object | The global revocation (protocol §6.2), signed by the person key, verbatim. Every workspace refuses the device from then on. |
| `policy.changed` | P | `gates`: {gate: policy} | The workspace defaults. |
| `settings.changed` | P | `set`: {`grant_hours?`: int 1–24, `claim_ttl_min?`: int 15–1440, `lease_ttl_min?`: int 5–1440, `repos?`: {repo name: {`path`: str} or null}} | **D60** (`grant_hours`). Existing grants keep their end time. `null` removes a repo. |
| `grant.issued` | P | `grant`: grant id; `scope`: `all` or `workable`; `verbs`: `"agent"` or [operation name]; `issued_at`: timestamp; `hours`: int; `expires_at`: timestamp; `secret_hash`: hash; `label?`: str | **D60** terms in §10.1 A3. Always for the signer. `expires_at` = `issued_at` + `hours`; the host refuses an `issued_at` more than 300 s from its clock. |
| `grant.revoked` | P | `grant`; `reason?`: str | **D60.** |
| `addon.granted` | P | `name`: addon name; `version`: str; `package_sha256`: hash; `capabilities`: [token] | Owner only. Also enables the addon. |
| `addon.disabled` | P | `name` | Data stays untouched. A new `addon.granted` enables it again. |
| `addon.purged` | P | `name` | Deletes the addon's data. |

The **member-list version** (`list_seq`) is the number of `member.added`, `member.removed` and `role.changed`
events in the workspace log so far. A signed event cites the version its signer saw. When a member is removed,
their earlier signatures stay valid against the version they cite; their unused approvals stop counting.

**Who may sign what** (on top of the actor column):

| Events | Who |
|---|---|
| `gate.*`, `verdict.given` | eligible approvers of that gate (§5.7) |
| `ticket.closed`, `ticket.reopened`, `people.changed`, `visibility.changed`, ticket `policy.changed` | the ticket owner, workspace owners and maintainers |
| `member.added`, `member.removed` | owners; maintainers for the roles member and viewer |
| `role.changed`, workspace `policy.changed`, `settings.changed`, `addon.*`, `restore`, `workspace.created` | owners |
| `device.added`, `device.revoked` | the device's own person (a revocation is signed by their person key) |
| `device.removed` | the device's own person; owners for any device |
| `grant.issued`, `grant.revoked` | §10.1 A3 (D60) |

**Unknown types.** The prefixes `ticket`, `status`, `visibility`, `people`, `policy`, `claim`, `task`, `handoff`,
`log`, `question`, `gate`, `verdict`, `branch`, `artifact`, `edit`, `projection`, `restore`, `workspace`, `member`,
`role`, `device`, `settings`, `grant` and `addon` are reserved: no addon may take one of these names, and an
unknown type under one of them is refused. `<addon>.<verb>` is accepted only from a granted, enabled addon (or
through its command group), and is checked against the envelope plus the addon's manifest.

**Not events.** Refusals (`human_only`, stop rule) are kept in the session records in `.state/`, not in a log.
Workspace views, agent starts, relay links, epochs and terminal events are defined with their phases (P2, P3).

### 5.5 Signing by the host, and the chain

- **`host_sig`** = `Sign(WSK, "orch/v2/sig/host-event|" || cj({"suite": 2, "workspace_id": W, "log": L, "event":
  E}))`, where `L` is the uid or `"workspace"`, and `E` is the full event without `host_sig` (so it includes `seq`,
  `at`, `prev` and `sig`).
- **Event head:** `head(e) = "sha256:" + hex(SHA-256("orch/v2/event|" || cj(e)))` over the full event, `host_sig`
  included, computed from the strictly parsed object, never from the raw line.
- **Chain:** `prev` of event `n` is `head` of event `n−1`; `prev` of `seq` 1 is `null`. The **log head** is the head
  of the last event. An empty log has no head.
- **Reading** a log: strictly parse each line, check that it is `cj`, check the field set, `seq`, `prev`, `ws_seq`
  and `host_sig`, then `sig` against the device certificate, then replay authorization (§5.11). A line that fails
  breaks the chain: every read and `orch doctor` report `chain.broken` with its `seq`, and nothing after it counts
  until an owner-signed `restore`.
- **Writing** is atomic per event, under the store lock: (1) write the new `ticket.json`/`body.md` to
  `.state/pending/<event id>/`; (2) append the event line and fsync; (3) rename the pending files into place and
  fsync the directory. **Crash recovery**, at the next lock: if the last event's hashes don't match the files and
  its pending files exist and match, finish step 3; if they are missing, report `store.torn_write` (the prose for
  that event is lost; `ticket.json` is rebuilt from events). Pending files without their event are deleted. A file
  change without an event is never treated as the host's write.

### 5.6 Hashes

Every hash in the format is `sha256:` followed by 64 lower-case hex characters, artifact digests included. A hash
is parsed (one parser, which refuses anything else) and compared as bytes. A digest is only compared together with
the field it belongs to; no code looks a hash up by value alone.

| Hash | Definition (`H` = SHA-256) | Used in |
|---|---|---|
| artifact digest | `H(file bytes)` | `artifact.*`, gate `artifacts`, `addon.granted` `package_sha256` |
| section hash | `H("orch/v2/section\|" \|\| UTF-8(section text))` | `ticket.updated`, `edit.external`, `base_rev` |
| value hash | `H("orch/v2/value\|" \|\| cj(value))` | `base_rev` for `ticket.json` paths |
| gate hash | `H("orch/v2/gate\|" \|\| cj(G))`, `G` in §5.7 | `gate.*`, `verdict.given` |
| policy hash | `H("orch/v2/policy\|" \|\| cj({"gate": gate, "policy": P}))`, `P` the effective policy (§5.7) | `gate.*`, `verdict.given`, gate hash |
| genesis | the event head of `workspace.created` | the trust root (§5.11) |
| people hash | `H("orch/v2/people\|" \|\| cj({"owner", "assignees", "reviewers", "watchers"}))`, lists sorted, owner a person id or null | gate hash |
| question hash | `H("orch/v2/question\|" \|\| cj({"question_id": "Q1", "ticket": uid, "text", "options"}))` (protocol §13's shape; `options` `[]` when none) | `question.*` |
| event head | `H("orch/v2/event\|" \|\| cj(event))` | `prev`, `based_on`, checkpoints |
| grant secret hash | `H("orch/v2/grant-secret\|" \|\| secret bytes)` | `grant.issued` |

- The artifact digest is the one unlabelled hash: it must match `sha256sum` of the file. It is never compared with
  any other kind of hash.
- **Refuse, don't normalise.** Every hash function refuses an input string that breaks the text rules (§11.3)
  instead of normalising it. Normalising happens once, when text enters the store.
- New labels (`orch/v2/gate|`, `section|`, `value|`, `policy|`, `people|`, `event|`, `grant-secret|`,
  `sig/ticket-event|`, `sig/ws-event|`, `sig/host-event|`, `sig/checkpoint|`) go into the orch-relay
  `vectors_v2.json` label list; the table stays prefix-free.

### 5.7 Gates

**Gates.** The core has four, in this order: `requirements`, `plan`, `verify`, `code`. Addons can't add gates;
their fields and sections join one of these.

**Policy** (workspace default and ticket override):

| Key | Type | Rule |
|---|---|---|
| `approvers` | list of approver tokens, at least one | who may approve |
| `count` | int ≥ 1 | how many distinct persons must approve at the current hash and generation |
| `not` | list of approver tokens | excluded, even when also in `approvers` |
| `applies` | `"all"`, `"off"` or a list of ticket types | default `"all"`; `code` defaults to `"off"` (D59) |
| `independent` | bool | default `false`; `true` adds the independence rule below. Always `true` for `code`. |

- **Approver tokens:** the workspace roles `owner`, `maintainer`, `member`, and the ticket roles `ticket_owner`,
  `assignees`, `reviewers`, `watchers`. `owner` always means the workspace role; the ticket's owner is
  `ticket_owner`. A viewer never approves.
- **The effective policy is an intersection,** recomputed whenever either side changes: `approvers` = workspace ∩
  override, `count` = the larger, `not` = the union, `independent` = either, and the gate applies when either side
  says it applies. An override can therefore never end up looser, even after a later workspace change. An override
  that would leave no eligible approver token is refused; if a later workspace change empties the set, the gate is
  blocked (`gate.no_eligible`) until someone fixes the policy.
- For hashing, `approvers` and `not` are sorted, `not` is `[]` when absent, and `applies` and `independent` are
  always present.
- For `code`, `not` always includes `assignees` and `independent` is `true`; the host refuses a policy without them
  (D59).

**Generations.** Each gate of a ticket has a generation `gen`, starting at 0. It goes up by one on:
- a change request on this gate or an earlier one, and a `fail` verdict (for `verify` and `code`);
- `ticket.reopened` and `restore` (every gate);
- `gate.invalidated` for this gate;
- any event that changes this gate's input `G` (below): an edit or `edit.external` of a bound path, an artifact
  event or receipt (`verify`), `branch.pushed` (`verify`, `code`), `people.changed`, a policy change of this gate
  (workspace or ticket, while the ticket is not `done` or `closed`), an addon grant or disable that changes bound
  fields, and a change in the counting approvals of an earlier gate.

The generation is derived by replaying both logs (ordered by `ws_seq`), so it needs no content, only events. A
decision (`gate.approved`, `gate.changes_requested`, `verdict.given`) carries the `gate_gen` its signer saw, inside
the signed bytes. **A decision whose `gate_gen` is not current is refused at append and never counts on replay.**
So a delayed approval can't land after a later rejection, and **a voided approval is retired for good**: reverting
the content brings back the old hash, but not the old generation.

**Gate hash input `G`.** These 15 keys are always present, for every gate (empty values where a gate doesn't use one):

| Key | Value |
|---|---|
| `workspace_id` | 32 hex |
| `uid` | the ticket uid |
| `gate` | the gate name |
| `schema` | `"orch.ticket/2"` |
| `hash_v` | `1` |
| `sections` | {section id: text} for each section of the gate (table below) that this ticket's type has; `""` when it is missing |
| `fields` | `{"ticket_type", "size", "acceptance": [{id, text}], "addons": {addon: {field: value}}}`; `addons` holds the fields whose manifest `gate` names this gate, a missing value as `null`; `{}` when none |
| `addon_packages` | {addon: `package_sha256` of its current `addon.granted`} for every addon with a field or section in this gate; `{}` when none |
| `tasks` | plan gate: `[{id, text, verify, proves}]` in `ticket.json` order; every other gate: `[]` |
| `artifacts` | {name: {`kind`, `digest`, `ac`, `task`}} (`ac`, `task` `null` when absent): for `requirements` and `plan` the file artifacts referenced inline in the gate's sections; for `verify` every file artifact in the manifest; for `code` `{}` |
| `receipts` | `verify`: {task id: {`event`: id of the `task.done`, `repo`, `commit`, `exit`}} for every done task with a receipt; every other gate: `{}` |
| `source_sha` | `verify` and `code`: the source list (below); every other gate: `[]` |
| `prior` | {earlier gate: {`gen`, `approvals`: [event ids]}} for each earlier gate that applies: its generation and the ids of the first `count` counting approvals by `seq`, sorted; `{}` for `requirements` |
| `policy_hash` | the policy hash of this gate |
| `people_hash` | the people hash |

| Gate | Sections |
|---|---|
| `requirements` | `summary`, `context`, `requirements`, `out_of_scope`, plus addon sections with this gate |
| `plan` | `plan`, `decisions`, plus addon sections |
| `verify` | `verification` (`findings` for a spike), plus addon sections |
| `code` | none (`{}`) |

Addon artifacts (`addon` + `ref`) have no digest: they are not bound, the prompt marks them "not bound", and they
never count as evidence.

**The source list (D58, D59).** One entry `{"repo": repo identity, "ref": "refs/heads/<branch>", "sha": git commit
id}` per repo in `links.repos`, sorted by `repo`. Every repo a ticket names is code-bearing and must have a branch
in `links.branches` before `submit`. The **repo identity** is the canonical URL of the working copy's `origin`
remote (`https://<host>/<path>`: host lower-case, no credentials, no port 443, no trailing `/` or `.git`; ssh forms
mapped to this form), or `local:<repo name>` when there is no remote. The host reads identity, ref and sha from git
(`settings.repos`), never from an agent, when it builds the prompt, **again when it appends the decision** (a
mismatch is refused with `gate.stale`), and again at landing.

**Who may approve.** An approval (`gate.approved` or a `pass` verdict) counts only if the approver:
- is a current member with an eligible token, and not excluded by `not`;
- signed the current gate hash, the current `policy_hash` and the current `gate_gen`;
- and, when the policy is `independent`: was not an assignee since the gated content last changed, and had no agent
  edit that content on their behalf.

`independent` is off by default for `requirements`, `plan` and `verify`, so a sole owner can approve the work of
their own agents (§12 N12). It is always on for `code`.

A gate is approved when `count` distinct persons have counting approvals. A gate that doesn't apply to the ticket's
type is not needed. **`done` is recomputed after every event:** when a `done` ticket's `verify` (or `code`, where it
applies) no longer has its count at the current generation, the host appends `gate.invalidated` and the ticket goes
back to `testing`. A policy change does not move `done` or `closed` tickets, except as D59 says for `code`.

**Change requests.** `gate.changes_requested` on gate G, or a `fail` verdict, raises the generation of G and of every
later gate, so all their earlier approvals stop counting. The ticket goes to `in_progress` when G is `verify` or
`code`.

**Text in approved content.** The approval is refused (`gate.suspicious_text`) when gated text contains a bidi
control (U+202A–202E, U+2066–2069, U+200E, U+200F, U+061C). Other invisible characters (U+200B–200D, U+2060,
U+FEFF, tag characters, other `Cf`) are shown as `⟨U+200B⟩` in every approval prompt and in `orch show`
(§12 N5).

**The verdict binds to the commit (D58).** The verify gate hash includes the source list, and the prompt shows the
commits and the diffstat. **Any change of a ticket ref's value** (a new commit, a rebase, a force-push, a reset, also
back to an older commit) is a new head. When the host sees one (on any `orch` call in P1, from the host process in
P2), it appends `branch.pushed`; the generation of `verify` and `code` goes up; and when they had counting
approvals it appends `gate.invalidated` (`new_commits`) listing them. A `done` ticket goes back to `testing`.

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
- **`base_rev`** maps each path the edit touches to the hash the editor last saw: the section hash for `body.*`, the
  value hash for `ticket.*`. The host refuses the edit with `conflict.section` (exit 8) when one of them is no longer
  current. Orch tracks it per session and path; agents never pass it.
- **External edits.** A `body.md` change the host didn't write becomes `edit.external` with the new section hashes.
  It voids every gate whose hash it changes. If the file broke the text rules, the host rewrites it normalised and
  sets `normalised: true`. **Any** change to `ticket.json` the host didn't write is reverted, with
  `projection.repaired` naming the paths (§12 N6).

### 5.9 Status

`backlog`, `open`, `in_progress`, `testing`, `done`, `closed`. Status is derived from events:

| Event | Status |
|---|---|
| `ticket.created` | `open` |
| `status.changed` | `open` ↔ `backlog` (not while claimed) |
| `claim.taken` | `in_progress` |
| `claim.released` (not after done or close) | `open` |
| `ticket.submitted` | `testing` (needs `requirements` and `plan` approved where they apply, and evidence for every AC) |
| `verdict.given` `pass` reaching `count`, and `code` approved where it applies | `done` |
| `verdict.given` `fail`, `gate.changes_requested` on `verify` or `code` | `in_progress` |
| `gate.invalidated` (`verify`) | `testing` |
| `ticket.closed` | `closed` |
| `ticket.reopened` | `open` |

"Waiting" (a blocking question is open, or a gate waits for a person) is derived and shown, not a status.
On a `done` or `closed` ticket the host refuses agent edits of bound content (`ticket.updated` of a gate-bound path,
`artifact.*`, `task.*`): `ticket.reopened` comes first. Notes, questions and person events stay possible. A claim
lapses when its session appended nothing for `claim_ttl_min`, or when its grant ends; a task lease lapses after
`lease_ttl_min`. The host appends `claim.released` (`expired` or `grant_ended`) with the next write.

### 5.10 Checkpoints and restore

The host keeps WSK-signed checkpoints in `.state/checkpoints/` (P1) and publishes them to the relay and to member
devices from P3:

| Kind | Fields |
|---|---|
| ticket | `{"v": 2, "suite": 2, "kind": "ticket", "workspace_id", "uid", "seq", "head", "at", "host_sig"}` |
| workspace | `{"v": 2, "suite": 2, "kind": "workspace", "workspace_id", "genesis", "n", "at", "workspace_log": {"seq", "head"}, "tickets": {uid: {"seq", "head"}}, "host_sig"}` |

- `host_sig = Sign(WSK, "orch/v2/sig/checkpoint|" || cj(checkpoint without host_sig))`. `n` counts the workspace
  checkpoints from 1.
- Refused: a checkpoint with a lower `seq` (or `n`) than one already seen, and **one with the same `seq` but a
  different `head`** (`chain.diverged`). A log whose head at a checkpointed `seq` differs from the checkpoint is
  diverged too: reads report it and no new events are appended until a `restore`.
- **Restore.** After a rollback (a restored backup, a git force-push of the workspace repo) the owner signs
  `restore {from_seq, head, abandoned, reason}`: `from_seq` and `head` name the last event on disk that stays
  valid, and `abandoned` names the highest checkpoint that is given up (`null` if none). The host appends it as
  `from_seq + 1` with `prev = head`, records the abandoned checkpoint, and accepts checkpoints of the new chain from
  then on. Every gate's generation goes up, so no decision from the abandoned part counts again.
- **What P1 can't detect:** if both the history and the local checkpoints in `.state/` are replaced, the rollback
  is invisible. From P3, checkpoints on the relay and on member devices catch it.

### 5.11 Trust root and authorization replay

- **Genesis.** The trust root of a workspace is the head of its `workspace.created` event, which carries the
  owner's delegation (signed by the owner's person key, protocol §7.1). The host remembers it outside the workspace
  (`<host state dir>/hosts/<workspace_id>/genesis`), puts it in every workspace checkpoint, and shows its
  fingerprint when a device pairs, so the device pins it. A workspace log whose first event doesn't match the
  remembered genesis is refused (`trust.genesis_mismatch`).
- **Readers replay authorization, not only signatures.** For every event, in `ws_seq` order, a reader checks:
  the actor kind may append the type (§5.2, §5.4); the person's device certificate chains to a member's person key,
  is in scope, and was not removed or revoked at that point; the person held the role the event needs at that
  point; an agent's grant was valid, in scope and covered the verb; the policy and generation allowed the decision;
  and the status transition was allowed (§5.9). An event that fails doesn't count, and the reader reports it.
- **What this protects in P1.** An agent that can use the workspace key (core §4) can append agent and host events
  with a valid `host_sig`. Replay stops those events from granting anything a person didn't sign: a forged
  `member.added`, policy, grant or decision fails because it needs a person's signature. It can't stop a forged
  agent event that cites a real, valid grant (the grant secret is not in the log); that limit holds until P2 moves
  the workspace key into the host (§12 N4).

### 5.12 What host events may do

| Host event | Exact effect |
|---|---|
| `branch.pushed` | records a new ref value; raises the generation of `verify` and `code` |
| `gate.invalidated` | records voided approvals; raises that gate's generation; moves `done` → `testing` (verify, code) |
| `edit.external` | **installs** the new `body.md` prose as the current text and records its section hashes; raises the generation of every gate whose input changed |
| `projection.repaired` | rewrites a projection (`config.json`, `ticket.json`, `keys.jsonl`) back to what the events say; changes no state |
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
- **Inline use in prose:** `![alt](artifact:after.png)`. A gated section that shows an artifact binds its digest.
- **Evidence:** an acceptance criterion has evidence when a **file** artifact names it in `ac`, or a `done` task
  that `proves` it has a receipt with `exit` 0. Addon artifacts are never evidence. Both are bound in the verify
  gate hash (§5.7).

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
  narrower grant, for example for CI). A human-only operation is never in a grant.
- Length is whole hours; `expires_at` = `issued_at` + `hours`, both signed. A `settings.changed` of `grant_hours` doesn't shorten
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
| `gate.invalidated` cause | `new_commits` (D58), `content_changed`, `policy_changed`, `conflict_resolved` (D53, P2) |
| `projection.repaired` cause | `external_edit`, `projection_mismatch`, `keys_mismatch` |
| `auth` | `passphrase`, `secure-enclave`, `secure-enclave-unlocked`, `webauthn`, `tpm`, `windows-hello` |
| grant scope | `all`, `workable` |
| grant verbs | `"agent"` or a list of operation names |
| core artifact kind | `screenshot`, `log`, `report`, `link`, `dataset`, `build`, `diagram`, `receipt`, `feedback`, `other` |
| manifest field type | `string`, `text`, `integer`, `boolean`, `enum`, `string_list`, `person` |
| addon capability | `serve_http`, `spawn_agent`, `pty`, `network`, `git_push` |

### 11.5 Frozen in P1, and the test vectors

Frozen with F1 (a change is a new `hash_v` or signed-event contract `v`): the signed bytes (§5.3, §5.5, §5.10), the
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
| `ticket_event_sig`, `ws_event_sig` | signed bytes and a P-256 signature; replay into another ticket or workspace refused |
| `chain` | 3 events: heads, `prev`, `host_sig`; a tampered line; a non-`cj` line |
| `generation` | a delayed approval after a change request refused; a reverted edit doesn't revive an approval |
| `checkpoint` | lower `seq` refused, equal `seq` with another head refused, a restore |
| `repo_identity` | ssh and https remotes mapped to one canonical URL |

## 12. Needs owner

These points change or sharpen the security model. Each was challenged by an adversarial Codex review; the final
wording below is what the text above follows. They wait for the owner's confirmation.

| # | Point | Final wording | Codex |
|---|---|---|---|
| N1 | Is every person event signed? | **Every event with a person actor is signed**, whatever its type (§5.2). Otherwise an agent holding the workspace key (P1, core §4) can forge "Severin commented …". | agreed with Codex |
| N2 | `presence` or `auth`; the P1 factor | `auth` replaces `presence` as **custody metadata**, covered by the signature but not a proof of the factor; no certificate field is invented (§5.3). P1 uses `passphrase` (D65). The weaker guarantees stay explicit: D49 (an unlocked iPhone signs) and D65 (a passphrase typed where an agent can read it is not protected). This weakens D41 for P1 and needs the owner's yes. | agreed with Codex, modified |
| N3 | Which key signs what | Person events: the **device key**, with certificates in the workspace log. The person key signs device certificates, revocations and the workspace delegation (protocol §6, §7.1). `device.removed` (this workspace, protocol §6.3) is separate from `device.revoked` (global, the PK-signed revocation, protocol §6.2). | agreed with Codex, modified |
| N4 | `ORCH_GRANT` as a bearer secret | `gr_<ULID>.<secret>`, only `secret_hash` in the signed `grant.issued`. Printed once on the person's terminal, never written to a file by orch, redacted from orch's output and records, stripped from the environment of every process orch starts. Grants are always for their signer. **Stated limit:** in P1 an agent that can use the workspace key can forge agent events citing a valid grant; replay can't catch that until P2 moves the key into the host (§5.11). | agreed with Codex, modified |
| N5 | Invisible and bidi characters | Refuse bidi controls, C0/C1 controls (except LF, TAB) and code points unassigned in **Unicode 16.0** (pinned) in all ticket text; refuse an approval whose gated text has a bidi control; show other invisible characters as `⟨U+…⟩` (§11.3, §5.7). | agreed with Codex, Unicode pinned |
| N6 | External edits of `ticket.json`; questions | Every external change to `ticket.json` is reverted (`projection.repaired`); only `body.md` takes external edits. `question.asked` carries the **full question**, so `ticket.json` can be rebuilt from events. | agreed with Codex, modified |
| N7 | Verify gate, verdicts and stale decisions | A verdict **is** the verify decision (`pass`/`fail`). Every gate has a **generation**; decisions sign `gate_gen` and `based_on`; a stale generation is refused and never counts; change requests, `fail`, reopen, invalidation, restore and input changes raise it; later gates bind the earlier gates' generations and approvals (`prior`). | agreed with Codex, modified (generation barriers) |
| N8 | Host events | Not "only take away": `edit.external` **installs** prose. §5.12 lists the exact effect of each host event and states that a host event never approves, answers, grants, adds a member or creates a ticket. | Codex was right; changed |
| N9 | Unattended writes | Keep A3 (`ask`, `log`, `artifact add`) and change the harness plan's H2. Unattended writes only on tickets with `visibility: workspace`, with a per-session quota (30 events, 20 MiB of artifacts per hour). An unattended `artifact.added` can void a pending verify approval; accepted and visible. | agreed with Codex, modified |
| N10 | The code gate (D59) | `not` always includes `assignees` and `independent` is always `true`, enforced by the host. Only a person's signature approves it; no `via` field in P1. In P1 turning it on moves no ticket back. | agreed with Codex |
| N11 | Addon writes | Addons hold no key; the host appends their events. `set_by` is a list of actor alternatives checked per write (`agent`, `addon`, human tokens = a signed person event). Only leaf paths can be set, so replacing `ticket.addons.<addon>` can't bypass `set_by`. Addon events never carry core authority. | agreed with Codex, modified |
| N12 | Independence of approvers | The rule "not an assignee since the content changed, and no agent edit on their behalf" is the policy option `independent`, **off by default** for `requirements`, `plan` and `verify`, so a sole owner can approve their own agents' work. Always on for `code` (D59). | new from the Codex review |
| N13 | Trust root | The head of `workspace.created` (with the owner's delegation) is the genesis: remembered by the host outside the workspace, in every workspace checkpoint, shown at pairing. Readers replay authorization, not only signatures (§5.11). | new from the Codex review |
| N14 | Rollback detection in P1 | Equal-height divergent heads are refused and `restore` is defined, but **P1 can't detect a rollback when both the history and the local checkpoints are replaced**; relay checkpoints (P3) fix it. | new from the Codex review |

## 13. Decisions log (F1)

Sources: #335 Q1–Q7 (PR body), #335 CR (code review 6067420402), #335 SR (security review 6067462572), #336
A1–A20 (PR body), HO (dashboard handover, input only), D58–D60, and the adversarial Codex review of F1 (rows 56–80).

| # | Gap (source) | Decision | Reason |
|---|---|---|---|
| 1 | Event names for implied events (#336 A1, HO) | The list in §5.4: 30 ticket-log and 13 workspace-log types. Renamed: `log` → `log.added`, `handoff` → `handoff.written`, `session.granted` → `grant.issued` (+ `grant.revoked`), `claim.expired` → `claim.released` (`expired`); from HO: `member.role_changed` → `role.changed`, `gate.policy_set` → `policy.changed`, `people.set` → `people.changed`, `labels.changed`/`section.edited` → `ticket.updated`, `lease.*` → `task.started` plus derived lapse, `task.run` → `task.done` receipt, `comment.added` → `log.added`, `workspace.grant_hours_set` → `settings.changed`, `device.paired`/`device.removed` → `device.added`/`device.revoked`. `agent.refused` is not an event. HO's other workspace types wait for their phases. | One type per meaning; every core type is `<noun>.<verb>`, so it can't collide with an addon's `<name>.*`. |
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
| 22 | Imported v1 history (#336 A20) | Deferred to C10, which adds its event types by amendment. | Depends on what v1 history the importer keeps. |
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
| 35 | NFC and the Unicode version (SR 9) | Refuse unassigned code points; at least Unicode 14.0. | NFC is stable only for assigned code points. |
| 36 | What callers enforce (SR 10) | Roots are objects; sizes are checked before parsing (§11.2). | Written down once for every caller. |
| 37 | `policy_hash`/`people_hash` unchecked (CR 4) | Both are defined labelled hashes (§5.6) and validated as hashes. | A gate hash can't be built from arbitrary strings. |
| 38 | Missing signature labels (SR 2) | `orch/v2/sig/ticket-event\|`, `sig/ws-event\|`, `sig/host-event\|`, `sig/checkpoint\|`. | Each signer and object kind has its own domain. |
| 39 | Signing context form | `label \|\| cj({v, suite, workspace_id, log, event})` instead of `…\|<workspace_id>\|<uid>` fields; `v` is the signed-event contract version (1). | Protocol §2.4 style; no delimiter rules; the suite and contract version are bound. |
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
| 52 | `list_seq` | The member-list version the signer saw (count of member events). | Signatures stay valid against the list they cite. |
| 53 | Where device certificates live | The workspace log (`device.added`); workspace removal and global revocation are separate events. | Every reader can verify human signatures offline. |
| 54 | Event line bytes | Each line is exactly `cj(event)`. | Hashing the parse and the line agree; a hand edit is caught. |
| 55 | D61, D62 | Not in the format; no `via` field, no mandate events. | Out of P1 (owner, 10 Oct). |
| 56 | `ticket.created` payload `type` collides with the envelope (Codex 1) | Renamed `ticket_type`; also in the gate hash `fields`. | One name, one meaning per object. |
| 57 | `auth` checked against a certificate field that doesn't exist (Codex 2) | `auth` is signed metadata only; no certificate field; signed-event contract versioned as `v`. | Don't invent protocol fields; protocol §6.1 is fixed. |
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
| 80 | Device removal vs revocation (Codex N3) | `device.removed` (workspace) and `device.revoked` (global, PK-signed revocation). | Protocol §6.2 and §6.3 are two different things. |
Open after F1 (not settled here):

- orch-relay protocol §13 uses a 16-byte hex `question_id`; the format uses `Q1`. P3/P4 must define the mapping
  for phone answers.
- orch-relay `vectors_v2.json` needs the vectors of §11.5. That is an orch-relay change.
- Bundled Unicode 16.0 tables for Python 3.11–3.13 (C1).
- Imported v1 events (C10), addon event payloads and the `needs` language (C9).
- D63–D66 are still drafts; `auth` (N2) depends on them.
