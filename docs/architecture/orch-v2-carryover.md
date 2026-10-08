# orch v2: what we carry over

Decided by the owner on 8 Oct 2026, from an inventory of 110 features in orch-core, orch-tix and orch-publish
(code, tests, open issues, and real usage counts). The interactive checklist with the evidence per feature is
[orch v2 Carry-over Checklist](https://claude.ai/artifact/LxHjG8gUDKsV9QYQwBtafc).

**"Take over" means rebuilt in v2 with the old code and its tests as reference, never copied as is.** v2 is a fresh
codebase on `develop` (D36). Today's code stays on `main` for daily use until the cutover.

A feature counts as done only when its e2e scenario passes (spec §23).

> **Phases changed (D42, 8 Oct 2026):** wave 1 is now built across P1 Core (CLI and model) and P2 Frontend (the
> Mission Control pages); wave 2 is P2 Frontend. See [orch-v2-core.md](orch-v2-core.md) §7.

## Kernel wave 1 (P1a): the core you work with every day

Built first, on the new identity and key custody from P1.

| ID | Feature | Notes |
|---|---|---|
| C01 | Tickets: create, list, show | On the workspace UUID |
| C02 | Lifecycle, claim, move | Who may move: person ids |
| C03 | Log, current state, sections, labels, due dates | |
| C04 | Task lists and named checks | Signed checks use person keys |
| C20 | Artifacts (evidence) | Most used feature |
| C19 | Links: branch, PR, external key | |
| C15 | Epics (grouping, children, health) | Delegation comes in Phase 2 |
| C08 | `orch wait` / handoff | Redesigned result contract (#225) |
| C09 | Event log | Signed and sealed stream |
| C35 | Evidence checks | |
| C34 | Safe text rendering | Includes the cap from #259 |
| C05 | Questions to the human | Lifecycle reworked in P4 |
| C06 | Approval gates and request changes | |
| C07 | Verdict, close, reopen | |
| C26 | Config, schema, rules | |
| C25 | `orch check` and `init` | `init` creates the UUID and the keys |
| C27 | Instructions sync | |
| H03 | Session-start hook | |
| S01 | Skills: tickets, work on ticket, refine | Rewritten |
| D01 | Dashboard shell | **New look and feel** (owner note) |
| D03 | Live updates | |
| D04 | Today (needs you) | |
| D05 | Board and backlog | |
| D06 | Ticket page | |
| D07 | Ticket actions | |
| D08 | New ticket page | Ticket mode; Factory modes in Phase 2 |
| D20 | Workspace settings page | Reduced |
| D22 | Workspace switcher | Multi-workspace |

## Kernel wave 2 (P1b): workflow extras, after key custody

These start agents or touch shells, so they come once the host socket and key custody exist. "Agent UID on
macOS" (#287) moves forward from P8 into this wave, so terminals and agent starts never run with access to the
keys (D37).

| ID | Feature | Notes |
|---|---|---|
| D09 | Start agent from the dashboard | |
| D16 | Terminals | Behind agent UID; Remote typing via passkey (P3) |
| C18 | Worktrees | |
| C16 | Quick tasks | |
| C29 | Records commit and push | Rebuilt; the closed #254 is reference |
| C25d | Doctor, guided setup, setup skill | For v2 `init` |
| C28 | Commit check and hook installer | |
| C30 | Update and harness update | |
| C31 | Feedback queue | |
| D11 | Activity, timeline, agents | |
| D17 | Widgets (rich ticket sections) | Keep the sandbox design |
| D19 | Guide | New text |

## In their v2 phases (relay, mobile, publish)

| Phase | Taken over |
|---|---|
| P2 relay | T21 security headers, CSP and rate limits; T16 presence (with P3) |
| P3 mobile (now P4, native app per D45: these TIX items are **UI reference only**, no code is ported) | T49 PWA shell, service worker and offline; T38 workspaces page (it becomes the home cards); T37 ticket page on the phone (rebuilt); T39 remote dashboard frame; T42 streams viewer (#96); T43 unlock sheet and passkeys (#97); T17 settings (reduced); T36 needs-you screen (with P4) |
| P4 push | T18 push (VAPID worker) |
| P5 Drop | T02 encrypted files, expiry and ack; T07 public links; T08 upload links; T60 sharing CLI verbs as the host Drop client; T30 files list, preview, upload, paste and voice notes; T34 voice transcription (opt-in, D25); T35 encrypted outbox |
| P7 publish | P01p shares (public, secret, sealed); P04 share lifecycle and backups; P05 app runtime; P07 stacks and templates; P09 CLI with staging and show-once links; P13 Mission Control pages and decisions (needs the addon runtime, so it lands with Phase 2's A01) |

## Phase 2: after the v2 build

| ID | Feature |
|---|---|
| D25 | AI Factory runner, rebuilt. The closed Dark stack #79→#143 is reference (D38). |
| D25d | Dark profile and signed switch. Peer auto-start (D28) depends on it. |
| C13 | Permits (agent asks, human grants) |
| C15d | Epic delegation and auto-approve |
| T44 | Factory status and approvals on the phone (#100) |
| A01 | Addon runtime and API, with a smaller API |
| C12w | Friendly guard warnings (not a security boundary) |
| C32 | Related, graph export, search |
| B05 | ticket-usage addon |
| T45 | Terminal client on the phone (TIX PR #99) |
| T46 | Widgets sandbox on the phone |

## Later / maybe

Each of these needs a reason to come back:
- **Guard hook:** C12, the deny-list version, parked rather than discarded.
- **Plugins and pages:** P01 orch-session status line, D12 reports, D13 graph page, D15 schedules and recurring
  tickets.
- **Addons:** B06 model-routing, B01 github-reviews, B02 github-issues, B04 wiki.
- **TIX features:** T15 agent messages (they fit ws→ws), T06 tags and short ids (tags must be sealed first).

## Replaced by the v2 design

The old code is reference only:

| ID | Old feature |
|---|---|
| C10 | HMAC ledger |
| C11 | Actor detection |
| D02 | Dashboard token auth |
| R01–R06 | Remote bridge host, pairing v1, route scopes, typing lease, Factory and terminals over the bridge, remote e2e harness |
| D21 | LAN phone pairing |
| B11 | orch-tix addon |
| T01 | TIX accounts and master key |
| T09 | Device onboarding |
| T13 | Spaces, mirrors, decisions, notify switch |
| T20 | Bridge v1 (protocol v2 is done) |
| T22 | TIX infra (now in the dev kit) |
| T40 | Pairing screen v1 |
| P08 | SSH deploy path |

## Dropped

| ID | Feature |
|---|---|
| C03s | Sprints |
| C33 | `orch migrate` |
| D18 | Design gallery |
| B07 | Switch-only addons (they become plain settings) |
| B03 | databricks addon |
| T11 | Legacy TIX tickets |
