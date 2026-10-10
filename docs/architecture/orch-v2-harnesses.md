# orch v2: harnesses and enforcement tiers

Status: **draft for the owner's review**, 10 Oct 2026. It writes down what the spec already implies (A1–A4, D3,
D55) and adds a test plan. It decides no new product behaviour.

## 1. Position

orch is an **agent-agnostic CLI** with a **Claude Code plugin** as one way to install and wire it. The core never
depends on a harness:

- Every command is defined once in the operation registry (A1). Any agent that can run a shell command can use it.
- Gates, roles, policies and signatures are enforced by the CLI and the host (A2, D3), not by a harness hook.
- An agent's identity is `ORCH_GRANT` and `ORCH_SESSION` (A3, A4): plain environment variables.
- Instructions are `AGENTS.orch.md` (neutral) and `SKILL.md` skills (portable format, D55).

Supported harnesses are those that pass the test in section 4: Claude Code and Codex CLI first, then GitHub
Copilot CLI. A harness is not "supported" until its row in section 4 has passed.

## 2. What is harness-specific

| Piece | Claude Code | Other harnesses |
|---|---|---|
| Install | plugin marketplace | `uv tool install`, then `orch init` writes `AGENTS.orch.md` |
| Instructions | `AGENTS.orch.md` and skills | `AGENTS.orch.md` (Codex reads `AGENTS.md`; Copilot reads its own instruction files), skills where the harness supports `SKILL.md` |
| Session-start and pre-compact hooks | plugin hooks | none; the agent runs `orch status` first (stated in `AGENTS.orch.md`) |
| Guard hook (`PreToolUse`) | optional second layer | not available |

## 3. Enforcement tiers

| Tier | Needs | Prevented | Only detected |
|---|---|---|---|
| **A. CLI and signatures** (every harness) | the CLI, `ORCH_GRANT` | approving or giving a verdict without a human signature; human-only verbs; acting without a grant (only `ask`, `log`, `artifact add` work, marked `unattended`); two sessions on one task | direct edits of `ticket.json`, `events.jsonl` or `body.md` (external-edit detection in `store/`, shown by `orch doctor` and `orch check`) |
| **B. Host socket under a separate UID** (where the platform allows, D3) | `orch serve`/`orch host`, OS groups | everything in A, plus agent reads and writes of workspace files and keys | none beyond the compromised endpoint (non-goal) |
| **C. Harness guard hook** (Claude Code today) | the plugin | hand-editing ticket status, human-only commands and workspace-forbidden git actions, before they run | n/a |

Rules that follow:

- A harness without tier C must still keep every **gate** (approval, verdict, answer) safe. Those depend on the
  signature, so tier A holds them. What a hook adds is early refusal of file edits.
- A workspace that must prevent direct file edits for non-Claude agents uses tier B. The docs say so plainly instead
  of implying the hook is the whole guard.
- `orch doctor` reports the tier the current session runs in.

## 4. Harness test plan

One scripted scenario, run per harness against a throwaway workspace with a **stub or scoped grant**. It extends
the P1 e2e scenarios (spec §23); a harness passes when every step below behaves as stated.

| # | Step | Expected |
|---|---|---|
| H1 | Load the harness with the orch instructions and skills | the agent runs `orch status` and names its grant |
| H2 | Agent runs `orch new` and `orch task add` | a ticket exists, events signed, `unattended` marking correct with and without `ORCH_GRANT` |
| H3 | Agent runs `orch ask` then `orch wait`; a human answers in another terminal | `wait` returns `answered` with the cursor, and the agent continues without guessing |
| H4 | Agent tries `orch approve` and `orch verdict` | refused as human-only, with the exit code and message from the error envelope |
| H5 | Agent tries to edit `ticket.json` directly | tier A: detected by `orch check`; tier B: write denied by the OS; tier C: blocked by the hook |
| H6 | Agent proposes `done` with a task lacking evidence | refused; with evidence and artifacts, ticket reaches testing |
| H7 | Parallel subagent or second session takes the same task | second lease refused (A4) |
| H8 | A skill with an `orch.skill.json` sidecar and a connection reference | the connection is not usable until the owner approves it (D55) |
| H9 | Output of `orch --json` parsed by the agent | `orch.cli/2.0` envelope read without prompting hints |

| Harness | H1–H9 | Tier verified | Date | Notes |
|---|---|---|---|---|
| Claude Code | not run | | | |
| Codex CLI | not run | | | |
| GitHub Copilot CLI | not run | | | not installed on the dev machine yet |

The test needs the P1 CLI (ops for `new`, `task`, `ask`, `wait`, `approve`, `verdict`, `check`). Until those exist
it stays a plan; the P1 task group that finishes the CLI owns running it.

## 5. Out of scope here

- API-only agents (own agent loop on the Messages API). Not decided; the MCP addon (A1) is the route that would
  serve them, and it comes later.
- Changing the skills' content or the hooks' behaviour.
