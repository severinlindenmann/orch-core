# orch v2: the minimal core

Status: **decided by the owner**, 8 Oct 2026 (D42–D44). This is the build plan for the first v2 phase. It implements
[orch-v2-ticket-format.md](orch-v2-ticket-format.md) (T1–T16, A1–A5) and nothing beyond it.

## 1. What the core is

The core is what an agent and a person need when nothing else is installed:

- **The `orch` CLI.** It is written for agents first: terse output and as little context as possible.
- **The workspace format:** `config.json`, `keys.jsonl`, and per ticket `ticket.json`, `body.md`, `events.jsonl` and
  `artifacts/`.
- **The agent instructions:** `AGENTS.orch.md` (at most 25 lines), three short skills, and a session-start hook.
- **The addon mechanism.** Everything optional plugs in here, the dashboard included.

The core has **no web server and no daemon** (A2). The CLI runs the operation registry in-process, writes behind a
file lock, and plays the host's role. It holds the workspace key, signs appends, and enforces gates, roles, policies
and visibility. In P2 the same registry moves into a host process behind a socket. Commands and output don't change.

Everything in the format is real in the core, not just stubbed (A5). Two exceptions wait for later phases:
- the relay transport, so checkpoints are written locally until P3;
- the UI, which belongs to the dashboard addon in P2.

## 2. Package layout

The core is rebuilt fresh on `develop` (D36). v1 on `main` stays the reference for behaviour and tests.

```
plugins/orch-core/src/orch/
├── schema/        JSON Schemas: ticket, body sections, event, workspace, addon manifest, operation I/O
├── canon/         JCS (RFC 8785), NFC/LF text normalisation, hash helpers (hash_v)
├── crypto/        the suite (P-256 per D46), sealing, signatures, domain labels
├── custody/       key backends: Secure Enclave on Apple-silicon Macs (user presence), keychain, libsecret, file (VPS)
├── identity/      person key, device certificates, recovery code, session grants, member list
├── store/         Store.append (the only write path), file lock, keys.jsonl, events, checkpoints,
│                  projection repair, external-edit detection, .state/index.sqlite
├── model/         derive state from events: lifecycle, gates and policies, people and roles, claims and
│                  task leases, questions, tasks and AC, artifacts, visibility, needs
├── ops/           the operation registry: one module per operation, with its schema, who may run it,
│                  preconditions, and the events it appends
├── cli/           parser generated from the registry, text and JSON renderers, error envelope, exit codes,
│                  stop rule, retry dedup, describe/help
├── instructions/  AGENTS.orch.md generator, skills, session-start and pre-compact hook text, stale check
├── addons/        manifest loader, out-of-process runner (JSON-RPC over stdio), capability grants,
│                  field/section/kind registration, needs-rule evaluator
└── importer/      v1 → v2 import (tickets, history as imported events, artifacts)
```

Rules that keep it small and safe:

- **Only `store/` writes files.** No other module opens a ticket file for writing. (In v1, writes were spread
  across modules; that is why external edits and races were hard to catch.)
- **`model/` is pure.** It derives state from events and is unit-tested without a filesystem.
- **`ops/` is the contract.** Adding a command means adding one op module, which brings its schema, permission and
  events. The CLI, `orch describe`, `orch help` and a later MCP addon are generated from the registry.
- **Python 3.11+, `cryptography` for crypto, no YAML dependency.** The dashboard's dependencies (FastAPI and
  others) belong to its addon package, not to the core.

## 3. The operation registry

Each operation declares:

| Field | Meaning |
|---|---|
| `name` | e.g. `task.done` (CLI: `orch task done`) |
| `input` | JSON Schema of the arguments (positional and flags are generated from it) |
| `who` | `agent` (needs a grant), `unattended` (works without a grant: `ask`, `log`, `artifact.add`), `human` (needs a person's signature with user presence), or `read` |
| `pre` | preconditions, checked against derived state (for example: the session holds the claim; the task has no other lease) |
| `emits` | the event types it appends |
| `output` | the text template (one `ok …` line plus at most one `next:` hint) and the JSON shape |
| `errors` | the error codes it can return, each with hint and fix |

The commands are the ones in format §10.3. The registry is the single source for `orch describe <cmd>`, which an
agent only reads when it needs to.

## 4. Security model in the core (no daemon yet)

| Threat on a single Mac, before the host and agent UID exist | Answer in the core |
|---|---|
| An agent signs as the human | Human-only operations need the device key, which sits in the keychain with user-presence access control: every signature shows a Touch ID prompt naming the action and hash. An agent can't produce one. |
| An agent edits ticket files directly | `store/` detects it through `rev` and section hashes. Prose changes become `edit.external` and void the gates they touch; protected fields are reverted with `projection.repaired`. |
| An agent appends forged events by hand | Each appended event carries `host_sig` and `prev`. A line without a valid `host_sig` breaks the chain, and `orch doctor` and every read report it. |
| An agent uses the workspace key itself (it can read the keychain item the CLI uses) | Accepted for the core and stated plainly: the agent could forge agent-level events, but never a human signature, because human events need the separate presence-protected device key. P2 moves the workspace key into the host process. |
| An agent acts beyond its grant | Grants are signed by the person and checked on every write. `who: agent` operations refuse without a valid grant. |
| History truncated or rolled back | Local checkpoints in `.state/` and, from P3, signed checkpoints on the relay. A rollback needs an owner-signed `restore`. |

## 5. Tests and the fast loop

| Tier | What | Budget |
|---|---|---|
| T0 | One module's tests, e.g. `uv run pytest tests/model/test_gates.py -x -q` | under 30 s |
| T1 | The touched package (`tests/store`, `tests/ops`…) plus lint | under 3 min |
| T2 | The whole core, shared vectors, golden CLI outputs, and the core e2e scenarios in orch-dev-kit | under 25 min |

The test assets:

- **Shared vectors.** The protocol v2 vectors in orch-relay cover signatures, canonical JSON and sealing. The core
  tests against the same vectors, so the CLI and the relay can't drift.
- **Golden CLI outputs.** For every operation, the exact text and JSON for a fixed workspace. They catch output
  bloat as well as breaking changes.
- **Property tests for `model/`.** Random event sequences must never produce an impossible state: two claims, an
  approved gate on a changed hash, a done task without a lease holder.
- **Context budget tests.** `AGENTS.orch.md` stays at 25 lines or fewer; the session-start text at 6 lines or fewer;
  a default `orch show` stays under a fixed token count.

## 6. Task groups for the core phase

Each group is one PR into `develop` (or a short stack), and ends with T2.

| Group | Content | Depends on | Review |
|---|---|---|---|
| C1 Schemas and canon | All JSON Schemas, JCS, text normalisation, hash helpers with `hash_v` | — | Opus security |
| C2 Crypto, custody, identity | Suite, keychain with user presence, file backend, person key, device certificates, recovery code, session grants, member list | C1, S1 spike | Opus security |
| C3 Store | `Store.append`, file lock, `keys.jsonl`, events with `prev`/`host_sig`, checkpoints, projection repair, external-edit detection, index | C1, C2 | Opus security |
| C4 Model | State derivation: lifecycle, gates and policies, people and roles, claims and task leases, questions, tasks and AC, artifacts, visibility, needs | C1 | Opus security (gates, policies) |
| C5 Registry and CLI contract | Registry, generated parser, renderers, error envelope, exit codes, stop rule, retry dedup, `describe`, `help` | C1 | — |
| C6 Agent operations | `status`, `next`, `show`, `list`, `search`, `new`, `claim`, `release`, `handoff`, `submit`, `ask`, `wait`, `set`, `section`, `ac`, `task`, `artifact`, `log`, `apply`, `inbox` | C3, C4, C5 | — |
| C7 Human operations | `approve`, `request-changes`, `verdict`, `answer`, `close`, `reopen`, `grant`, `member`, all with user presence | C2, C6 | Opus security |
| C8 Instructions | `AGENTS.orch.md` generator, the three skills, session-start and pre-compact hooks, stale check, `init` | C6 | — |
| C9 Addon mechanism | Manifest, out-of-process runner, signed capability grants, fields, sections, artifact kinds, needs rules, inactive data | C3, C4, C5 | Opus security |
| C10 Import and doctor | `import v1` (tickets, history, artifacts), `doctor`, `check`, a commit check | C6 | — |
| C11 Core exit | The core e2e scenarios, context budget tests, and the owner's daily use on one real workspace | all | — |

C1, C4 and C5 can start in parallel. C2 waits for spike S1, which tests Secure Enclave P-256 keys and their interop with the shared vectors.

## 7. New phase order

The owner's build order: **core → frontend → relay → mobile → apps → the rest.**

| Phase | Was | Content |
|---|---|---|
| **P0 Dev foundations** | P0 | The dev kit and VPS, spikes S1/S2, the e2e harness. (The Dark Factory stack is no longer part of it.) |
| **P1 Core** | P1a, part of P1b | This document: C1–C11. |
| **P2 Frontend** | parts of P1a, P1b | The host process: the registry behind a socket, holding the workspace key. Agents get their own OS user on macOS (#287). The dashboard addon (FastAPI, desktop only, new look and feel). The first-party addons that need a UI or start agents: start agent, terminals, worktrees, quick tasks, records, activity, widgets, guide, doctor/setup, update, feedback. Ends with the cutover (D44). |
| **P3 Relay** | P2 | orch-relay directory and bridge v2, members and epochs, checkpoints on the relay, desktop-browser e2e, protocol change #24 (members' devices). |
| **P4 Mobile** | P3, P4 | The native iPhone app (D45, repo orch-mobile): pairing v2 with QR scan, workspace cards, questions and needs-you, APNs push with "answered elsewhere", Face ID signatures from the Secure Enclave, Drop basics with a share extension (D43). TestFlight; iPhone sessions 1 and 2. |
| **P5 Apps** | P7 | orch-publish: signed HTTPS API, namespaces, the dashboard pages. |
| **P6 The rest** | P5, P6, P8, Phase 2 list | Drop (files, inbox claim, documents, web agents), linked workspaces, colleagues and the internal relay, the AI Factory and Dark profile rebuild, the remaining addons, TIX switch-off, iPhone session 3. |

## 8. Decided with the order

- **D43 Drop basics in P4:** sending a file from the phone to a workspace, and from a workspace to the phone, comes
  with mobile. The rest of Drop stays in P6.
- **D44 Cutover after P2:** once the core and the dashboard cover daily work, `orch import v1` moves the owner's
  workspaces, and `develop` is merged into `main`. Relay, mobile and the rest continue on v2.
