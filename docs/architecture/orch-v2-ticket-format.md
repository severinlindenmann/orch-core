# orch v2: ticket and artifact format (draft)

Status: **draft for the owner's review**, 8 Oct 2026. This is the first artifact of the v2 build order:
**minimal core → workspace frontend → relay → mobile → apps → everything else.**

## 1. The minimal core

With nothing else installed, orch v2 is:

- the `orch` CLI (Python, no web server);
- the workspace folder with the ticket and artifact format below;
- `AGENTS.orch.md` and the instructions block, plus the core skills (tickets, work on ticket, refine);
- the addon mechanism, so everything else can plug in.

Everything beyond that is either a **built-in feature** that ships in the package and is switched on in config, or
an **external addon** in its own package that is installed and enabled. Both are toggles. Mission Control is the
first feature (`dashboard`), not part of the core.

## 2. Design rules

1. **One folder per ticket**, named by id. Status is a field, not a folder, so a move never renames files and two
   people changing different tickets never touch the same path.
2. **Text that people write lives in `ticket.md`.** What happened (claims, moves, approvals, answers, verdicts, log
   lines) is appended to `events.jsonl`, one signed event per line. Gate and verdict state is derived from the
   events, never stored twice.
3. **Artifacts live inside the ticket folder**, with a small manifest `artifacts.yaml`.
4. **Addons get their own namespace** in the frontmatter (`addons.<name>`) and their own events (`<name>.*`). Core
   never deletes data from an addon that is switched off.
5. **Everything machine-readable has a schema** (`orch schema ticket`), versioned `orch.ticket/2`.

## 3. Workspace layout

```
orchestrator/
├── config.yaml                  # workspace id, prefix, features and addons on/off
├── AGENTS.orch.md               # generated rules for agents
├── tickets/
│   ├── DEMO-0007/
│   │   ├── ticket.md            # frontmatter + sections (people's text)
│   │   ├── events.jsonl         # append-only, signed history
│   │   ├── artifacts.yaml       # manifest of evidence
│   │   └── artifacts/           # the files
│   │       ├── before.png
│   │       └── pytest.log
│   └── DEMO-0012/ …
├── addons/                      # workspace-level addon data (not per ticket)
│   └── publish/shares.yaml
└── .state/                      # git-ignored: index cache, locks, sessions
```

`config.yaml`:

```yaml
schema: orch.workspace/2
workspace:
  id: 6f1c0d2e-8b4a-4e1f-9c3d-2a7b5e9f0c11   # random UUID, created by `orch init`
  prefix: DEMO
  name: Acme energy data
features:            # built into the orch package, off unless listed true
  dashboard: true
  worktrees: true
  quick_tasks: false
  terminals: false
addons:              # separate packages; installed with `orch addon install`, toggled here
  estimate: {enabled: true}
  publish: {enabled: true, settings: {default_access: secret}}
  github: {enabled: false}
```

## 4. A ticket: `ticket.md`

Example (the demo's DEMO-0007, rewritten in the v2 format):

```markdown
---
schema: orch.ticket/2
id: DEMO-0007
title: Accept DD/MM/YYYY gateway timestamps
type: bug                 # feature | bug | chore | spike | investigation | epic
status: testing           # backlog | open | in-progress | waiting | testing | done
priority: high            # low | normal | high | urgent
size: s                   # xs | s | m | l
labels: [gateway]
parent: null              # epic id
blocked_by: []
due: null
resolution: null          # completed | wont-do | superseded | duplicate (when done)
links:
  repos: [acme-energy-data]
  branches: {acme-energy-data: fix/DEMO-0007-gateway-timestamp-formats}
  prs: [{repo: acme-energy-data, url: "https://github.com/acme/energy/pull/17"}]
  external: [{key: GH-13, url: "https://github.com/acme/energy/issues/13"}]
questions:
  - id: Q1
    text: Should we also accept DD.MM.YYYY (dots)?
    why: Two gateways may switch formats next quarter.
    options:
      - {key: a, label: No, only the two formats in the requirements}
      - {key: b, label: Yes, add it now, cost: +1 test}
    recommended: a
    blocking: false
addons:                   # one key per addon; validated only while that addon is enabled
  estimate: {points: 2}
---

## Summary

Some gateway exports write timestamps as DD/MM/YYYY HH:MM; ingest rejects them today.

## Requirements

- Gateway exports in DD/MM/YYYY HH:MM are accepted alongside ISO 8601.
- The alternate format is normalised before pydantic validation.

## Acceptance criteria

- [ ] AC1 `_parse_timestamp` accepts both ISO 8601 and DD/MM/YYYY HH:MM
- [ ] AC2 A file using the alternate format parses end to end
- [ ] AC3 Every other gateway's ISO 8601 timestamps are unaffected

## Out of scope

- Detecting further timestamp formats.

## Plan

1. Add `_parse_timestamp()` that tries ISO 8601 first, then DD/MM/YYYY HH:MM.
2. Use it for `read_at` before building `RawMeterRead`.
3. Regression test with an alternate-format file.

## Tasks

- [x] T1 Add `_parse_timestamp()` · verify: `pytest -q tests/test_ts.py`
- [x] T2 Use it for `read_at` · verify: `pytest -q tests/test_ingest.py -k read_at`
- [ ] T3 Regression file end to end · verify: `pytest -q tests/test_e2e.py -k ddmm` · proves AC2

## Verification

- AC1, AC3: `pytest -q`: 10 passed ([log](artifact:pytest.log))
- AC2: ![Alternate-format file ingested](artifact:after.png)

## Current state

Waiting for the verdict. PR #17 is green; merge after the verdict.
```

What changed compared with v1:

| v1 | v2 | Why |
|---|---|---|
| `tickets/<status>/DEMO-0007-slug.md` | `tickets/DEMO-0007/ticket.md` | Moves don't rename files; artifacts sit next to the ticket |
| `gates`, `claim`, `sessions`, `verify` in frontmatter | Derived from `events.jsonl` | One source of truth; nothing can be edited into "approved" |
| `## Log` in the body | `log` events | No merge conflicts on the body; signed |
| Task list in its own format | `## Tasks` with stable ids `T1…` and `verify:` / `proves ACn` | Readable in any editor; the CLI keeps ids stable |
| Acceptance criteria unnumbered | `AC1…` ids | Tasks, artifacts and verdicts can point at them |
| `artifacts` list in frontmatter | `artifacts.yaml` | Frontmatter stays short (Internal has 228 files) |
| `sprint`, `worktrees`, `move`, `needs`, `signed` keys | Gone, or derived | Sprints dropped; worktrees become a feature; the rest is computed |
| Extra keys at top level | `addons.<name>` | Safe toggling, no name clashes |

Core sections (always present, in this order): Summary, Requirements, Acceptance criteria, Out of scope, Plan,
Tasks, Verification, Current state. Gates bind the same sections as in v1 (requirements: Summary, Requirements,
Acceptance criteria, Out of scope plus `type` and `size`; plan: Plan). Addon fields and sections can join a gate
(§7).

An **epic** is the same file with `type: epic`, no Plan, Tasks or Verification sections, and children found through
their `parent`. Its approval (the charter) covers its own requirements and lists the children it covers.

## 5. History: `events.jsonl`

One JSON object per line, append-only, never rewritten:

```json
{"v":2,"seq":1,"at":"2026-10-02T07:43:05Z","type":"created","by":{"agent":"claude-code","session":"c7a7","for":"p_7f3a"}}
{"v":2,"seq":4,"at":"2026-10-02T07:47:10Z","type":"gate.approved","gate":"requirements","hash":"sha256:fa37…","by":{"person":"p_7f3a","device":"d_91c2"},"sig":"ed25519:…"}
{"v":2,"seq":5,"at":"2026-10-02T07:47:20Z","type":"claim.taken","by":{"agent":"copilot","session":"c7a7","for":"p_7f3a"}}
{"v":2,"seq":9,"at":"2026-10-02T07:47:40Z","type":"task.done","task":"T2","receipt":{"exit":0,"seconds":4.1,"commit":"a1b2c3d"}}
{"v":2,"seq":11,"at":"2026-10-02T07:47:55Z","type":"log","text":"PR #17 green; handing over for testing."}
{"v":2,"seq":12,"at":"2026-10-02T07:47:58Z","type":"moved","from":"in-progress","to":"testing","by":{"agent":"copilot","session":"c7a7","for":"p_7f3a"}}
{"v":2,"seq":13,"at":"2026-10-02T08:02:11Z","type":"estimate.set","points":2,"by":{"person":"p_7f3a","device":"d_91c2"},"sig":"ed25519:…"}
```

- **`by`:** either a person (`person` and `device`) or an agent (`agent`, `session`, and `for`, the person it works
  for).
- **Signatures:** human-only events carry `sig`, the device key's signature over the canonical event plus the
  previous event's hash. That is the v2 replacement for the HMAC ledger: approvals, answers, verdicts, closes and
  any addon field marked human-only. A gate counts as approved only while a valid signed `gate.approved` for the
  current hash exists.
- **Addon events** are named `<addon>.<verb>`. Core stores and verifies them like any other event.

## 6. Artifacts: `artifacts.yaml`

```yaml
- name: before.png
  kind: screenshot
  label: Ingest error before the fix
  sha256: 3f9a…
  size: 48213
  ac: AC2
  by: {agent: claude-code, session: c7a7}
  added: 2026-10-02T07:46Z
- name: pytest.log
  kind: log
  sha256: 8c01…
  size: 2201
  task: T1
- url: https://github.com/acme/energy/actions/runs/36979118504
  kind: link
  label: CI run on PR #17
- kind: receipt               # written only by `orch task done --run`
  task: T3
  run: {exit: 0, seconds: 12.4, commit: a1b2c3d, check: e2e-ddmm}
- addon: publish              # an addon-defined kind
  kind: share
  label: Before/after report
  ref: {share: s7k2}
```

- Each entry has exactly one source: `name` (a file in `artifacts/`), `url` (never fetched), or `addon` + `ref` (the
  addon resolves it).
- Core kinds: `screenshot`, `log`, `report`, `link`, `dataset`, `build`, `diagram`, `receipt`, `feedback` (the
  human's own images, human-only), `other`. Addons register more.
- Inline use in sections works as in v1: `![alt](artifact:after.png)`. A gated section that shows an artifact binds
  its sha256 into the gate hash.

## 7. How addons add parameters

An addon declares what it adds in its manifest, `orch-addon.json`. Example: a small `estimate` addon.

```json
{
  "name": "estimate",
  "version": "1.0.0",
  "requires_core": ">=2.0 <3",
  "ticket": {
    "fields": {
      "points": {
        "type": "integer",
        "enum": [1, 2, 3, 5, 8, 13],
        "label": "Story points",
        "set_by": "human",
        "gate": "requirements",
        "filter": true,
        "show": ["list", "board", "ticket"]
      }
    },
    "sections": [
      {"name": "Risks", "after": "Out of scope", "gate": "requirements", "for_types": ["feature"]}
    ],
    "artifact_kinds": [{"kind": "benchmark", "label": "Benchmark result"}],
    "needs": [{"kind": "estimate", "when": "status == 'open' and not points", "who": "human"}]
  },
  "cli": {"group": "estimate"},
  "skills": ["skills/estimate/SKILL.md"],
  "agents_md": "agents.md"
}
```

What each key does:

| Key | Effect |
|---|---|
| `fields` | Stored under `addons.estimate.*` in the frontmatter, validated against `type`/`enum`. Every change is an `estimate.set` event. `orch field set DEMO-0007 estimate.points 5` and `orch list --where "estimate.points>=5"` work without the addon shipping any code. |
| `set_by` | `human` (signed event, refused for agents), `agent`, or `any`. |
| `gate` | The field joins that gate's hash, so changing points after approval invalidates the approval like a text edit. |
| `filter` / `show` | Where the dashboard feature shows and filters it (ignored without the dashboard). |
| `sections` | Extra sections, in a set place, optionally gated and only for some ticket types. `orch check` reports a missing required one. |
| `artifact_kinds` | Extra kinds for `artifacts.yaml`. |
| `needs` | Adds "needs you" items with a small, safe expression language (fields, status, type; no code). |
| `cli`, `skills`, `agents_md` | A command group, skills and a paragraph in `AGENTS.orch.md`, added only while the addon is enabled. |

The same ticket with two addons enabled:

```yaml
addons:
  estimate: {points: 5}
  publish:
    shares:
      - {id: s7k2, access: secret, until: "done+7d"}
```

**Toggle behaviour:**

- **Switched off:** its frontmatter keys and events stay untouched, and `orch show` prints them as "inactive addon
  data". Its gated fields stop counting. Changing that is a visible re-approval, never silent.
- **Switched on again:** its data is validated again, and anything invalid is reported by `orch check`, never
  deleted.
- **Uninstalled:** the same as off. `orch addon purge <name>` removes its data, and only a human can run it.

Addons can't change the core statuses, the lifecycle, or the core sections. That keeps every workspace readable by
plain orch.

## 8. Open questions for the owner

1. **One folder per ticket** instead of status folders. Recommended: yes.
2. **History out of the Markdown body** into `events.jsonl` (the body no longer has a `## Log`). Recommended: yes.
   A readable log is still `orch show --log`, and the dashboard feature shows it.
3. **Tasks and questions:** tasks as a Markdown section with ids, questions as structured frontmatter. Recommended
   as drafted, because questions need exact hashing and tasks need to stay readable.
4. **Addon namespace `addons.<name>`** instead of top-level keys. Recommended: yes.
5. **Built-in features vs. external addons:** dashboard, worktrees, quick tasks, terminals, widgets and records ship
   inside the orch package as features; github, publish, estimate, usage and wiki ship as external addons.
   Recommended split as listed.
