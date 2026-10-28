# Handover: orch Mission Control mockup

State on 9 Oct 2026, **after the complete mockup** (iterations 1–3, the UX review rounds and the owner's wave-3
input). Branch `feat/dashboard-mockup`; PR #330 (iteration 1 and 2, up to 906a87b) was merged into `develop`
(9381ac6). The remote branch exists again (`origin/feat/dashboard-mockup`), and `origin/develop` is merged in; the next
PR goes against `develop`. This file tells the next agent where the mockup stands, what the backend
has to provide, and what comes next. The owner's review guide is [REVIEW.md](REVIEW.md); every non-trivial decision
is in [DECISIONS-LOG.md](DECISIONS-LOG.md) with how to revert it.

## Goal

A **complete, clickable frontend mockup** of the orch v2 dashboard addon (Mission Control), running on simulated
JSON data with working, simulated buttons. When the backend exists, swap the mock transport for real calls to the host.
**No component changes should be needed**: components only talk to `src/api/client.ts`.

- Desktop and small desktop only (≥1024 px); no mobile. Every page fits a 13" notebook (1440×900) with the terminal
  docked on the right (page area down to 720 px).
- Every element that comes from an addon is marked with the orange **A** (once per surface for dense repeated items).

## Decisions (owner, 8 Oct 2026). Do not reopen.

| Topic | Decision |
|---|---|
| Brand | **"A · Orbit"**: a ring with a teal orbiting dot (a ticket in the centre, an agent on its orbit). Graphite plus teal `#2fc6a3`, fonts Geist and Geist Mono. Drafts (A was chosen over B Ledger and C Prompt): [brand canvas](https://claude.ai/artifact/2X2GWxkW4LBwV38opB2CuC). |
| Theme | **Dark only.** No light theme and no toggle; make dark as good as possible. |
| Addon marker | An orange "A" badge (`AddonBadge`) plus an orange hairline frame (`AddonFrame`) on every addon contribution. **Orange is reserved for addons**, so never use it for anything else. |
| Shell | A dark sidebar holding the workspace switcher, core nav (Today, Board, Tickets, Agents), an "Addons" group, and settings, grant status and viewer at the bottom. It switches between wide and narrow (icon rail) with a button or the `[` key. The choice is remembered; without one, the rail is used below 1280 px. |
| Build | Real code, not a design canvas: React 19, Vite 8, TypeScript, shadcn/ui (Radix), Tailwind v4, TanStack Router (browser history, permanent URLs: see Routes), TanStack Query, lucide. Exact pins are in `package.json`. |
| Mock | An **in-process mock API** (`src/mocks/`: router, store, derive, fixtures). There is **no MSW or service worker**, because the claude.ai preview viewer blocks service workers. Buttons really change the mock state; the store persists to localStorage. |
| Addon UI | Addons describe their UI as **declarative JSON nodes** (stack, stat, kv, list, table, markdown, code, chart, form, button, link), validated with zod and rendered by core inside `AddonFrame`. Slots: `nav`, `today.card`, `ticket.panel`, `board.lane`, `board.card_field`, `settings`. Rich addon pages later go into a sandboxed iframe. |
| Core-only | Approvals, answers, verdicts and addon *decisions* are always rendered and "signed" by core, never by an addon node. |
| Order | It1: shell, Today, Board, Ticket (**done**). It2: tickets list and search, full ⌘K, New ticket, Agents (claims, leases, grants), workspace settings (members and roles, gate policies, addon manager with capability grants), full workspace switcher. It3: every addon. All three are done (9 Oct). The owner reviews after each iteration. |

**Later owner input (9 Oct), all built:** wave 3 A–K (terminal dock, group by epic, addon pages with tabs, usage by
model, wiki, the widget catalog, addon settings in a drawer, New ticket overlay and Quick ticket, Relay & devices,
Drop, Artifacts, motion), wave 3b (D53 Landing, D55–D57 Skills, Connections and simple auth) and wave 3c (every page
fits a 13" notebook with the right dock). Controller rulings that refine the table above (not reopening it): addon
markers once per surface for dense items (board chips, table cells), addon install is one signed "Grant and turn on".

**Owner decisions (10 Oct), all built** (REVIEW.md "Decided", DECISIONS-LOG "Owner decisions 2026-10-10"): the
verdict signs the commit and new commits void it; an opt-in `code` review gate; widget aliases removed and the
proposed types documented (`docs/widgets-v1-proposal.md`); members grant themselves agent grants; the factory charter
gives verdicts; Drop stays orch-only (strict D54); Connections, Watch live and Schedules Run now as built.

## Source of the data model

Read these on `origin/develop` (`git show origin/develop:docs/architecture/<file>`):

- `orch-v2-ticket-format.md`: §3 `ticket.json`, §4 body sections, §5 `events.jsonl` (envelope, signed events, gate
  hash), §6 artifacts, §7 ticket document (`orch show --json`), §8 addons, §9 visibility, §10 agent interface
  (§10.3 = the command list, i.e. the operations).
- `orch-v2-core.md` §3: the **operation registry** (name, input, who = agent / unattended / human / read, pre, emits,
  output, errors), names like `task.done`. The P2 host exposes the same operations behind a socket, so **the
  dashboard API is these operations**, not a new REST surface (see "Backend contract" below).
- `orch-v2.md`: decisions D1–D57 (D41 Touch ID on the Mac, D49 iPhone signing, D53 landing, D54 relay without pages).
- The v2 core on `develop` is still a skeleton: there is nothing implemented to mirror yet.

The demo data is fictional: "Acme energy data" (DEMO), Internal (INT) and Client VM (CLI). People: Severin (owner,
`p_sev`), Mara (maintainer, `p_mara`), Tom (viewer, `p_tom`). Richest ticket: **DEMO-0043**. Two datasets: Normal and
Busy day (`src/mocks/busy/`, seeded generator, ~150 tickets in DEMO, live script).

## What exists

Paths are under `src/`.

| Area | Where | Contents |
|---|---|---|
| Brand, tokens, motion | `brand/`, `styles/tokens.css`, `lib/motion.ts` | Dark tokens (brand teal, addon orange reserved), FLIP/fade helpers, all motion off under reduced motion. |
| API contract | `api/` | `types.ts`, `client.ts` (the only door to the mock), `transport.ts` (`createMockTransport` used, `createFetchTransport` ready), shared pure rules: `permissions.ts`, `gates.ts`, `grants.ts`, `addons.ts`, `attention.ts` (the one "needs you" count), `sections.ts`, `launch.ts` (argv + validators), `harnesses.ts`, `connections.ts` + `secrets.ts` (zod schemas, secrets parser, masking), `widgetCatalog.ts`, `shortcuts.ts`. |
| Mock host | `mocks/` | `router.ts` (every endpoint below), `store.ts` (event log, derive, persistence `orch-mock-v2`), `derive.ts` (state from events, `describeEvent`), `sessions.ts` (agent run scripts), `sim.ts`, `relay.ts`, `connections.ts`, `artifacts.ts`, `workspace-log.ts`, `addons/*` (one module per addon), `busy/` (Busy day). |
| Shell | `app/shell/` | Sidebar (wide/rail, rail by itself beside a squeezing dock, max 6 addon pages + "More addons"), top bar with the Demo data pill (Normal / Busy day, Reset demo, **Review tour**), ⌘K palette, `?` help sheet, New ticket overlay host, toasts, workspace switcher, viewer switch. |
| Review tour | `app/review/` | The owner's checklist of REVIEW.md scenarios with Go buttons (person, workspace, dataset). Mock only. |
| Terminal | `app/terminal/`, `app/terminal/dock/` | xterm.js over a fake PTY (`fakePty.ts`, all text through `clean()`, secrets masked), simulated Claude Code / Codex CLIs, the tmux-like dock (bottom/right, resizable, sessions per ticket, continue from summary). |
| Pages | `app/pages/` | Today (grouped inbox, new-since-last-look, sign feedback), Board (dnd, epic lanes, auto rails), Tickets (filters, saved views, epic groups, column folding), Ticket (next action, gates stepper, tabs, rail/Panels, widgets, landing state, connection blocks), New ticket (page + overlay + quick + simulated dictation), Artifacts, Agents (sessions, grants, refusals), Settings (General, Members, Gates, Relay & devices, Addons + drawer, Skills, Connections), AddonPage. |
| Addon UI | `addon-ui/` | Closed node vocabulary validated with zod (`nodes.ts`: stack, tabs, table with column priorities, list, form, chart, widget, decision, fold, popover, terminal, frame …), `AddonFrame`/`AddonBadge`/`PreviewChip`, slots, one action hook (`useRunAddonAction`) with core's confirm dialogs: `SignConfirm`, `SpawnConfirm`, `DecisionSignPrompt`, `DestructiveConfirm`, `OptionsConfirm`, `SecretDialog`. |
| Addons (18) | `mocks/addons/*`, `mocks/fixtures/addons.json`, `catalog.json` | Installed in DEMO: guide, start-agent, widgets, estimate, publish, github, usage, terminals, wiki, land. Catalog: worktrees, quick, records, activity, models, factory, schedules, drop (the Busy day installs all but drop in DEMO). |
| Ticket page density (R4) | `app/pages/ticket/` (`Overview.tsx`, `Rail.tsx`, `widgets/`) | Current state shows 2 widgets, then "N more widgets"; question prototypes point to Questions → Answer; the rail holds addon panels under one "Addons · N" heading and one Terminal panel; the breadcrumb leads back to where the ticket was opened. Points (Estimate on) or Size, never both. |
| Widgets, Usage, Busy day (R4) | `api/widgetCatalog.ts`, `api/widgetTemplates*.ts`, `mocks/addons/usage.ts`, `mocks/busy/` | Widgets gallery ("Where it's allowed" once per section); Usage tiles with one measure and one period each, scaled on the Busy day; Apps & shares vs Drop say what each is for; Busy day widgets on 20 tickets covering every core widget type and two templates, every artifact kind. |
| Signing surface | `addon-ui/SignConfirm.tsx`, `DecisionSignPrompt.tsx`, `DestructiveConfirm.tsx`, `OptionsConfirm.tsx`, `app/pages/settings/addons/GrantDialog.tsx`, `test/signing-surface.test.tsx` | Signing and confirm dialogs: core's words in the title, covers and confirm button (an addon named "Title (package id)"); every arg that is sent is a core line "Words (key): value" (in the covers, or a "Sends" list above the addon region), never inside the addon's region; the addon's own text (label, sentence, row name, decision question) only in a labelled dashed "From the addon" region, always in full (wrapped, scrolling, never cut). Every addon-controlled or signed string goes through `components/sign/visible.tsx` (`Raw`/`visible`/`plain`/`Inline`/`Prose`/`RawValue`): bidi-isolated; control, format, every default-ignorable (U+034F, U+3164, variation selectors …), private-use, unassigned and non-ASCII space characters as `\u{…}`; a backslash as `\\`; edge spaces as ␠ (a literal ␠ escaped); empty as `""`; every non-ASCII code point of a string NFC would change (decomposed Hangul, é as e + U+0301, the Ångström sign). The encoding is injective (collision tests in `visible.test.tsx`). Typed signed values (args, decision terms) use `RawValue`: strings quoted with `\"` escaped, numbers and booleans bare. Ticket signing (`SignDialog`) shows each signed section and field on its own (task text, assignee, verify command, proves; question, option, answer text), with a provenance caption. Non-plain values (objects, NaN, Infinity), keys outside `^[A-Za-z][A-Za-z0-9_]{0,31}$` and more than 12 args fail closed. The confirm buttons of the destructive and options dialogs are core's ("Confirm: …", "Continue: …"); option field and choice labels sit in a labelled addon region. Package names must match `^[a-z][a-z0-9-]{0,39}$` and titles must be 1–40 characters without `( ) · :` or invisible characters (`manifestProblem` in `api/addons.ts`; the host refuses the install with 409 `addon.invalid_manifest`, the grant dialog will not sign). The table test covers 19 paths: gate approve, answer, verdict, addon decision, addon sign, options, destructive, start agent, agent grant issue and revoke, skill credential grant, addon install and update, relay connect, pair.confirm and device.remove, member role, gate policy, agent grant length. Not in the table (same SignPrompt pattern, own tests): relay stop, member add/remove. **Explicitly unsigned exception:** terminals `login_shell` (Today's "Log in in the terminal"): owner-only and needs the pty grant, but no signature and no confirm, because it runs nothing: it opens a shell as the connection's `run_as` with the login command typed, and the person presses Enter. The host still appends a `terminal.shell_opened` event (owner as actor). |
| Tests | `**/*.test.ts(x)`, `test/` | About 1,790 vitest tests (jsdom): contract, visibility sweep, orange guard, a11y smoke, signing surface, per page and per addon. `scripts/layout-guard.mjs` for real layout (below). Local only: CI does not run the dashboard suite or the layout guard. |

## Backend contract (for the host)

Every endpoint the mock serves (`src/mocks/router.ts`), with the operation it should become. Operation names are from
format §10.3 / core §3 where one exists; **"proposal"** means no operation exists yet and the name is our suggestion.
All workspace reads are members only (404 unknown workspace, 403 non-member); hidden tickets answer 404
`not_visible`; errors are `{code, message, hint}` like format §10.4.

**Identity and workspaces**

| Endpoint | Operation |
|---|---|
| GET `/api/me` | no operation yet — proposal `whoami` (the agent side has it inside `status`) |
| GET `/api/workspaces` (with `needs_you` per workspace) | no operation yet — proposal `workspace.list` |
| GET `/api/workspaces/:ws/identity` | no operation yet — proposal `workspace.show` (uuid, prefix, epoch, key fingerprint) |
| GET `/api/workspaces/:ws/cursor` | `status` (it shows the cursor) |
| GET `/api/workspaces/:ws/people` (owners) | `member` (list form) — proposal `member.list` with the directory |
| POST `/api/workspaces/:ws/settings` `{op}`: `member.add`, `member.role`, `member.remove` | `member` (human only, signed; format events `member.added`, `role.changed`) |
| same, `grant.hours` `{hours}` | owner only, signed in the UI (like `member.*`; the host requires the signature): whole hours 1–24 else 400 `validation.hours`; sets the workspace default grant length `grant_hours` (also the longest a member signs); format-less event `workspace.grant_hours_set` (provisional). Existing grants keep their end time |
| same, `gate.policy` | no operation yet — proposal `policy.set` (format event `policy.changed`; signed). Gate `code` (the opt-in code review) also takes `applies: 'off' \| 'all' \| [ticket types]` and always `not: 'assignees'` |
| same, `rename` | no operation yet — proposal `workspace.rename` |
| same, `archive` (always 409 `cli_only`) | no operation yet — proposal `workspace.archive` (CLI only) |

**Today, tickets and views**

| Endpoint | Operation |
|---|---|
| GET `/api/workspaces/:ws/today` | `inbox` (needs-you per person; plus `read_only_open`, `waiting_on_others`) |
| GET `/api/workspaces/:ws/tickets?q&status&type&parent…` | `list` / `search` |
| POST `/api/workspaces/:ws/tickets` | `new` |
| POST `/api/workspaces/:ws/tickets/:key/undo-create` | no operation yet — proposal `ticket.discard` (creator, nothing happened yet; key never reused) |
| GET `/api/tickets/:key` | `show --json` (the ticket document, §7) |
| GET `/api/tickets/:key/events?since=` | `show --log --since N` |
| POST `/api/tickets/:key/actions` `answer` | `answer` (human, signed). Carries `hash`, the question's hash the dialog showed (and the exact option and trimmed text it showed); 409 `question.stale` unless it is the question's hash now, compared in the step that records the answer; `question.answered` records it |
| … `approve` / `request_changes` / `verdict` | `approve` / `request-changes` / `verdict` (human, signed). `verdict` carries `source_sha` (the branch head the person saw; 409 `verdict.stale` otherwise) and signs it; `approve {gate: 'requirements' | 'plan', hash}` binds the content the person reviewed: `hash` is the gate's content hash (full sha256 of the material `gateSignedContent` covers) when the dialog opened, required, 409 `gate.stale` unless equal to the hash now (compared atomically with appending `gate.approved`, which records it); `approve {gate: 'code', source_sha}` is the code review (after a pass verdict, the same commit, never an assignee; 409 `gate.stale` / `gate.not_open`); request changes on `code` also voids the verdict |
| GET `/api/tickets/:key/changes` | no operation yet — proposal `diff` (core's diff of the ticket branch against its base: files, +/−, unified lines; the ticket document carries `branch` with head, commits and diffstat) |
| … `ask` | `ask` |
| … `comment` | no operation yet — proposal `comment` (closest: `log`, which is the agent's) |
| … `claim` / `release` | `claim` / `release` (agents only; the dashboard never offers them) |
| … `set_status` | `close` / `reopen` for leaving or returning from done (`done` itself is refused here: only a verdict reaches it); intermediate moves (backlog, open, in progress, waiting, testing): no operation yet — proposal `move` (owners/maintainers) |
| … `add_label` | `set REF labels=…` |
| GET/POST `/api/workspaces/:ws/views`, POST `…/views/:id/delete` | no operation yet — proposal `view.save` / `view.delete` (dashboard-only data) |
| GET `/api/workspaces/:ws/artifacts?kind&ticket&by&since&q&page` | `artifact list` is per ticket; the workspace-wide, paged listing is a proposal `artifact.search` |

**Agents and grants**

| Endpoint | Operation |
|---|---|
| GET `/api/workspaces/:ws/agents`, `…/agents/activity` | no operation yet — proposal `session.list`, `session.refusals` (refusals are the session's CLI error envelopes, not log events) |
| GET `/api/workspaces/:ws/agents/launch?ticket&mode&harness&where` | no operation yet — proposal `session.preview` (core-computed facts for the start dialog) |
| (start/stop run through the start-agent addon actions) | no operation yet — proposal `session.start` / `session.stop` (human; needs `spawn_agent`, a grant, a server-issued single-use confirmation) |
| GET/POST `/api/workspaces/:ws/preview/mandates` | **PREVIEW ONLY — not part of the contract.** Served by the mock for the non-functional mandates preview (M1, updated in U2 to the wide mandate, `docs/concept-mandates.md`; ops `enable`, `disable`, `issue {orchestrator, days ≤ 30}`, `renew {days ≤ 30}`, `stop`, `review`, `revoke`); nothing is signed. A host implements nothing here until core specifies mandates (D62 draft, PR #340); the shape will change. |
| GET/POST `/api/workspaces/:ws/grants`, POST `…/grants/:id/revoke` | `grant` (human, signed; revoke stops its sessions). Terms by role (`grantTerms`): owners and maintainers scope `all`, 1–24 h; **members grant themselves** scope `workable` (the tickets they may work on), 1 h up to the workspace default (`grant_hours`, 8); viewers none (403). Wrong scope 403 `grant.scope`. Members revoke their own; owners revoke any |

**Skills, connections, relay (D54–D57)**

| Endpoint | Operation |
|---|---|
| POST `/api/workspaces/:ws/connections/:name/check` | `check` |
| POST `/api/workspaces/:ws/doctor` | `doctor` |
| GET `/api/workspaces/:ws/skills`, `…/connections`, `…/secrets` | no operation yet — proposal `skill.list`, `connection.list`, `secrets.show` (names and permissions only, never values) |
| POST `/api/workspaces/:ws/skills/:name/grant` | no operation yet — proposal `skill.grant` (human, signed credential grant) |
| GET/POST `/api/workspaces/:ws/relay` `{op}`: `connect`, `stop`, `pair.start`, `pair.cancel`, `pair.confirm`, `device.remove` | no operation yet — proposal `relay.*` / `device.*` (P3) |

**Addons**

| Endpoint | Operation |
|---|---|
| GET `/api/addons`, `/api/workspaces/:ws/addons`, `…/addon-catalog` | `addon …` (list). Every package response (also the addon-operation answer) is the **public package**: no `decisions` or other runtime/ticket-bound state; decisions reach a client only through the filtered `addon-decisions` (security review #5) |
| POST `/api/workspaces/:ws/addons/:name` `{op}`: `install`, `grant`, `update`, `enable`, `disable`, `uninstall` | `addon …` (grant/update/install-with-grant are signed: format `addon.granted`) |
| GET `/api/workspaces/:ws/addon-decisions` | no operation yet — proposal: part of `inbox` (addon decisions core renders) |
| GET `/api/workspaces/:ws/addons/:name/state[?ticket=KEY]` | no operation yet — proposal `addon.state` (the addon's view for this person; per-ticket for panels) |
| POST `/api/workspaces/:ws/addons/:name/actions/:id` | the addon's own command group (format §8 `cli`), run by the host; `confirm: 'sign'` and `decision: true` actions are signed by core (events `addon.action_signed`, `addon.decided`) |

The host runs only an action the **installed, granted manifest declares** (`actions[id]`, an own entry; 403 `addon.undeclared_action` otherwise, also for an implementation an update dropped from the manifest; security review #10). It requires core's `confirmed: true` on `confirm: 'sign'`, `'destructive'`, `'options'`, `'spawn_agent'` and decision
actions (409 `confirm.required`); a signed action carries at most 12 plain values (400 `validation`), and
`addon.action_signed` records exactly the signed args (uncut, without `confirmed`).

**Proposal for the addon contract: decision `terms`** (F3 workspace links, 2026-10-10). An `AddonDecision` may carry
`terms: Record<string, string | number>`: what answering authorises (a link's peer, comparison code, carrier, scopes
each way, expiry). Same limits as signed args: at most 12 plain finite values under `^[A-Za-z][A-Za-z0-9_]{0,31}$` keys;
anything else fails closed (core does not offer the decision; `validTerms` in `api/addons.ts`, applied in
`openDecisions`). Core shows each term in the decision's signing covers as its own line "Words (key): value" (core
humanises the key; the value through `visible.tsx` `Raw`, in full), posts them with the answer (`decisionBody`), and the
host refuses the answer with 409 `decision.closed` ("…or its terms changed", hint "Reopen it and check the terms again")
unless they equal the decision's terms now (`sameTerms`: same keys, values and types). `addon.decided` records them.
Every decision answer also carries `digest` (`decisionDigest` in `api/addons.ts`: sha256 over addon, id, action, ticket,
title, question, detail, options and terms, exactly as core's prompt showed them, also in its Details); the host refuses
it with 409 `decision.closed` unless it equals the digest of the decision now, and refuses a body whose `ticket` is not
the decision's (security review #3). The digest is **required** on every decision answer (409
`decision.digest_required` without one; tests post through `test/offered.ts`, which computes it from the decision as
offered). A decision action the manifest marks
`authorises: true` (the factory `permit`) must carry what it authorises as typed `terms` — a permit's are `command`,
`ticket`, `epic`, `scope` — else the host refuses the answer (409 `decision.terms_required`); such a decision is
answered once (a second answer: 409 `decision.closed`, from core's `addon.decided` record), and the permit action
runs the command from the matched decision's terms, never from live permit state (it refuses when the live permit no
longer matches them). A decision's signed text (title, question, detail, options, terms) must be stable while it is open: no countdowns
(a factory hold says "Delivery at <time>: <destination>"; core draws the minutes left from `hold.until`, outside the
digest). After any 409 on a decision answer, the client refetches the decisions, so the next choice snapshots the
current one. A decision action marked `deciders: 'eligible'` (factory `code_review`, minRole member) is answered by
whoever core's gate eligibility admits (`gateEligibility` under the code-gate policy: e.g. a member who is a reviewer
under "the ticket's reviewers"), never a viewer, the requester or the author; the host skips the owners-and-maintainers
floor for it, members get only such decisions from `addon-decisions`, and the handler checks eligibility again.
Repos `adopt` signs the observed `default_branch` with the name, path, remote and folder and refuses any change (409
`repos.changed`). The
id still binds the decision; terms make what it binds legible. On an addon page, a decision node's primary option is
disabled while a form on that page that names it (`guards: <decision id>`, with `cancel`) holds unsaved edits
("Unsaved changes to the terms: save or cancel them before you accept."); other options stay.

The host refuses package names that are core namespaces (`addon`, `gate`, `role`, `policy`, `edit`, `projection`,
`restore`, … : `CORE_EVENT_NAMESPACES` in `mocks/addons/registry.ts`; install answers 409 `addon.reserved_name`) and
accepts only `<name>.<verb>` records from an addon.

Addon action ids the mock implements (manifest `actions` sets roles; default member): publish `share`, `share_once`,
`copy_link`, `extend`, `revoke`, `start`, `stop`, `logs`, `redeploy`, `decide`; github `import`, `refresh`,
`approve`, `open`; estimate `set`; wiki `open`, `search`, `clear_search`, `close`, `edit`, `done`, `create`, `save`,
`link`; terminals `open`, `new`, `close`, `open_ticket`, `start`, `resume`, `login_shell` (owner only plus the pty grant, else 403 / 409 `terminals.no_pty`; every path that creates or attaches a person's shell — `new`, `open_ticket`, `start`, `resume`, `login_shell` — checks the pty grant in one place, `newShell`, security review #11; `open` on a running person shell needs it too, and without it no session is `interactive` (a transcript stays readable); body is only `{connection}`, never a command: the host takes the command from that connection's `login_hint` (404 unknown connection, 400 no login); the shell runs as the connection's `run_as`; the hint is written to the pty once on first attach with control characters and newlines stripped and no trailing CR, so nothing runs until the person presses Enter; the session view carries `prefill` and `run_as`; appends `terminal.shell_opened {connection, run_as, session}` by the owner; unsigned by design, see the signing surface row); start-agent `configure`, `start`, `stop`;
guide `open`; worktrees `filter`, `add`, `remove`, `open_terminal`; quick `add`, `claim`, `close`, `make_ticket`,
`decide`; records `commit`, `push`, `pull`; activity `apply`, `view_timeline`, `view_ticket`, `show_new`,
`clear_filters`, `show_older`; models `escalate`; factory `permit`, `pause`, `resume`, `watch`, `stop_watching`;
schedules `arm`, `disarm`, `run_now`, `open_run`, `finding`; drop `share`, `claim`, `download`, `remove`, `extend`,
`revoke`; land `enqueue`, `dequeue`, `resolve`, `mark_resolved`, `run_worker`, `stop_worker`, `open_checks`; and
`save_settings` (owner) on every addon with settings.

**Mock only (not part of the contract):** POST `/api/dev/reset {dataset?}`, GET `/api/dev/dataset`, POST
`/api/dev/viewer`, POST `/api/dev/relay/:ws` (simulate a dropped link / a phone scanning), POST
`/api/dev/tickets/:key/push` (the ticket's agent pushes a commit, any time: it simulates an agent push; the Changes
tab offers it while a verdict stands). **The real host must not have this route.**

**Core rules the host must keep (owner decisions 10 Oct):**

- **The verdict signs the commit.** The verify gate hash covers the branch head (`source_sha`) with the verification
  section, artifacts and criteria. `verdict.given` and the verify `gate.approved` record `source_sha`.
- **New commits void it.** When the ticket branch gets a commit other than the one a standing verify (or code)
  approval signed, core appends `gate.invalidated {gate, cause: 'new_commits', sha, reason: "New commits after the
  verdict: <sha>"}` as host for each, and a done ticket goes back to testing (the landing-resolution path). Any
  approval on another commit is voided, also a partial quorum, and only approvals of the current head count. The mock
  sees commits as `task.done` receipts and `branch.pushed {sha, branch}` (agent); **the host takes the head from git
  (content-addressed), never from a sha an agent reports.** A re-push of an older sha is not a new head.
- **Charter size.** The charter checks the size recorded when the child was created; the mock has no size edit at
  all. If the host adds one, it must refuse it on charter children (or keep using the creation size).
- **Code review gate (`code`).** Off by default; on per workspace or per ticket type. When it applies, a pass
  verdict keeps the ticket in testing until the policy's count of people (policy approvers, never an assignee, never a
  charter) approve exactly that commit; landing needs it on the commit the verdict signed. A landing resolution voids
  it with verify. A policy change re-reads tickets (owner ruling): turning it on or raising the count moves back
  **only** done tickets with an open landing (queued, checking or failed, by the landing addon's own records); a done
  ticket without one (landed, merged by hand or a pull request, no landing addon) counts as landed and stays done.
  Turning it off or lowering the count makes tickets waiting for a review done. The signed covers name the exact
  tickets (dry run: proposal `policy.preview`, mock `POST /api/workspaces/:ws/code-review-preview`). Request changes on verify is refused while a verdict stands (409
  `verdict.exists`).
- **Member grants (`workable`).** The host must check the person's visibility and role on every claim and start
  path (dashboard, CLI, addons), not only in the start dialog.
- **Landing uses the signed commit.** The land worker's `source_sha` is the verify approval's `source_sha`; an entry
  whose approval no longer stands for that commit leaves the queue.
- **Charter verdicts.** `autoApprove(key, 'verify', {charter, by})` (core, an agent under an active charter, a child
  of its epic, in testing): `verdict.given` and `gate.approved` with `via: 'factory_charter'`, `charter`,
  `charter_signed_by`, `source_sha` and no presence. Never for `code` (403 `human_only`), and only for children
  within the charter's size limit (`maxSize`; larger or unsized: 409 `charter.out_of_scope`). Every surface says
  "Verdict: via the factory charter — no person reviewed this".
- **Schedules "Run now"** is member-level and unsigned in the mock (it only reads and reports). Before any run that
  starts an agent, the real host checks the addon's `spawn_agent` grant and the person's own active grant.

## Events the mock writes

Format §5 (with §6 and §8) names 11 event types: `gate.approved`, `task.done`, `artifact.added`,
`artifact.replaced`, `edit.external`, `projection.repaired`, `restore`, `addon.granted`, `member.added`,
`policy.changed`, `role.changed`. Event types are **not settled** (open PR #336 proposes 23 more; issue #338 is
undecided). Everything below that is not in that list is **provisional**.

- In format §5: `gate.approved`, `task.done`, `artifact.added`, `addon.granted`, `member.added`.
- Provisional, same meaning as a format name: `member.role_changed` (format: `role.changed`), `gate.policy_set`
  (format: `policy.changed`). Rename when the host lands.
- Provisional, ticket: `ticket.created`, `people.set`, `labels.changed`, `section.edited`, `handoff.written`,
  `status.changed`, `claim.taken`, `claim.released`, `lease.taken`, `lease.released`, `task.run`, `task.blocked`,
  `task.skipped` (these two are derived by `derive.ts` but never written by the mock), `log.added`, `comment.added`, `question.asked`, `question.answered`, `gate.changes_requested`,
  `gate.invalidated` (also core's landing void, and `cause: 'new_commits'` with `sha` for new commits after a verdict),
  `verdict.given` (with `source_sha`; a charter verdict adds `via`, `charter`, `charter_signed_by`),
  `branch.pushed` (an agent pushed `sha` to the ticket branch). `gate.approved` may name gate `code` (the code
  review, with `source_sha`).
- **Refusals are not events** (format F1; `docs/architecture/orch-v2-ticket-format.md`): a refusal is a CLI error envelope
  (`code` + `hint`, format §10 / core §3 `errors`) returned to the agent, never a ticket-log entry. The mock keeps them
  as a `refusals` list on the agent session rows (`fixtures/me.json`, `busy/agents.ts`); the Agents page and Ticket
  History read them from there (`GET /api/tickets/:key/refusals`, `agents/activity`). History draws them apart under
  "From the agent session — not part of the signed log": not in the event list, its counts or Raw.
- Provisional, workspace log: `ticket.discarded`, `workspace.renamed`, `member.removed`, `view.saved`,
  `view.deleted`, `grant.issued`, `grant.revoked`, `agent.started`, `agent.stopped`, `addon.installed`,
  `addon.enabled`, `addon.disabled`, `addon.updated`, `addon.uninstalled`, `addon.settings_saved`, `addon.decided`,
  `addon.action_signed`, `connection.checked`, `skill.credentials_granted`, `relay.connected`, `relay.stopped`,
  `device.paired`, `device.removed`, `epoch.rotated`, `workspace.grant_hours_set`, `terminal.shell_opened` (owner/maintainer-visible in Activity).
- Provisional, addon (`<addon>.<verb>` by `{kind:'addon'}`, format T11): `publish.shared`, `publish.revoked`,
  `publish.decided`, `github.imported`, `github.pr_linked`, `estimate.set`, `usage.recorded`, `wiki.linked`,
  `quick.made_ticket`, `records.committed`, `records.pushed`, `records.pulled`, `drop.shared`, `drop.claimed`,
  `drop.revoked`, `drop.removed`, `drop.extended`, `factory.paused`, `factory.resumed`, `factory.permit_granted`,
  `factory.permit_refused`, `factory.run_requested`, `factory.run_step`, `factory.deliver_held`,
  `factory.deliver_stopped`, `factory.delivered` (full runs, below), `land.queued`, `land.attempt`, `land.dequeued`, `land.resolved`.
- Not written by the mock: `artifact.replaced`, `edit.external`, `projection.repaired`, `restore`.

## Factory full runs: request fields, events, the hold/Stop contract (provisional)

Owner decision 10 Oct 2026 evening (D61 option); proposal `docs/factory-full-run-proposal.md`. In the mock it is the
factory addon's (`src/mocks/addons/factory-runs.ts`); the hold is meant to be **host-enforced**.

- **Request** (signed, `start_run`, `confirm: 'sign'`, maintainer+; a Deliver target owner-only in the mock): the
  signed args are exactly `request` (a single-use id the host issued at review: `rq-<PREFIX>-<nonce>-<n>`, the nonce new for every seeding of the state, so a reset never reissues an id), `goal` (≤ 200 chars), `goes_up_to` (`Preview` | `Deliver`), and for Deliver
  `deliver_means` (the concrete destination, ≤ 160 chars, required) and `hold_minutes` (integer 0–1440, default 15), plus `largest_child: 'm'`. Core records them in `addon.action_signed {args}`. The form (`prepare_run`) stores a
  per-viewer draft; the host refuses a signature whose values differ from the reviewed draft (409 `factory.stale`),
  a Deliver without a destination (400 `validation`), a run while the factory is paused or stopped (409
  `factory.not_running`), a reused request id (409 `factory.request_used`; the id is consumed with the run's creation in
  one step) and a run whose children do not fit the charter's child budget (409 `factory.budget`; admission reserves
  them, `used += 3`).
- **Charter:** one check at admission, every step and settlement: paused holds everything, the hold clock too; a
  charter stopped by time (or an over-committed budget) stops progress and ends a hold `factory.deliver_cancelled
  {run, reason: 'charter_stopped'}` ("Not delivered: the charter stopped"). Nothing is delivered after it stops.
- **Code review (D61 "never the code gate"):** when the workspace code review policy applies, each child waits at a
  Code review step for people: core decision `factory.code:<run>:<n>` (option `approve`, terms `{run, child,
  child_title, commit}`, signed in core's prompt; `addon.decided` with presence) → `factory.code_reviewed {run, child,
  commit, approvals, needed}`. Eligibility is core's D59 rule (`store.gateEligibility`, the same as a ticket's code
  gate): the policy's approver group, the run's requester and the child's author count as its assignees (never
  reviewers), one approval per person, and the policy's `count` of distinct people. The decision is offered only to
  people who are eligible. Each approval signs the child's commit; a new commit (`factory.child_pushed {run, child,
  commit}`) voids the approvals and sends the child back to wait before Validate (D58/D59). Validate and Preview wait
  for every review; the factory charter cannot satisfy it. An eligible mandate may (owner agreed 11 Oct), but the dashboard mandate is non-signing preview only.
- **Events** on the factory epic, by the addon: `factory.run_requested {run, request, goal, goes_up_to, children,
  deliver_means?, hold_minutes?}`, `factory.code_reviewed {run, child}`, `factory.deliver_cancelled {run, reason}`, `factory.run_step {run, step: 'Plan' | 'Preview'}`, `factory.deliver_held {run, deliver_means,
  until}`, `factory.deliver_stopped {run}`, `factory.delivered {run, deliver_means}`. Children's steps
  (Requirements, Build and test, Validate, Evidence) are in the addon state in the mock; a real host writes them on
  child tickets with `via: 'factory_full_run'`. Every decided step is labelled "via the factory full run <you|name>
  signed on <date> — no person reviewed this step".
- **Hold/Stop contract.** At Deliver the host opens a core decision `factory.hold:<run>` with **one option, `stop`**,
  `terms {run, deliver_means, hold_until}` and `hold {until, deliver_means}` (new optional `AddonDecision.hold`).
  Core shows it on Today, in place on the page (`decision` node) and as a calm shell line (`DeliveryHoldBanner`:
  "Delivering in 28 min · <deliver_means> · at 14:32 · AI Factory (factory) · Stop"). Stop is signed in core's
  decision prompt (`addon.decided`, presence Touch ID) and cancels: no `factory.delivered`, the run stays at
  Preview. When `until` passes with no Stop the host delivers to exactly `deliver_means` (it refuses any other
  destination, `factory.deliver_mismatch`) and writes `factory.delivered`. While the factory is paused nothing is
  delivered: Pause keeps what is left of the hold (`holdRemainingMs`, from the wall clock), and Resume rebuilds both
  deadlines from it, so a reload while paused never ends or shortens the hold. Agents never hold the delivery credential: the host delivers.
- **The factory stays on during a hold.** Disable, update and uninstall are refused (409 `addon.delivery_on_hold`,
  core's sentence names the run) while a delivery holds (mock: `MockAddon.offBlocked`). The real host keeps Stop
  available independently of the addon's activation (Stop is a core decision on a core-held deadline), so even an
  addon that crashed or was removed cannot take the Stop away.
- **Reloads:** the hold's deadline is held on the host's real clock. The mock keeps a wall-clock deadline next to the
  mock one (`holdWallUntil`) and re-derives the mock deadline from it when the mock clock restarts on a reload, so a
  reload never extends a hold. Seeded factory state is saved as soon as it is seeded (`MockAddon.saveOnSeed`), so the
  busy day's hold is not seeded again on a reload. Factory state version 5; version 4 migrates unchanged (new fields are optional); versions 2 and 3 are migrated
  (`MockAddon.migrate`): runs, holds and counters kept; a hold saved without a wall deadline gets its full window again
  from the reload (conservative: never shorter than what was left).
- Disable / update / uninstall initialise and settle the factory state before the hold check, so a seeded hold nobody
  looked at still blocks them.
- **No operation shortens an active Deliver hold.** During the hold the only human act is a signed Stop.
  “Deliver now…” exists only for a stopped run, under a fresh owner signature. The mock's "Simulate: let the hold time pass (demo)" is a simulator control of the demo data (owner
  only, refused outside the demo datasets), not a host operation: a host implements nothing for it.
- **Demo only:** "Fill in a demo request" (`demo_run`, prefills the form; still signed); the busy day seeds run R-1 on
  hold (28 min left); its 3 children are reserved in the budget (23 of 25 used), a valid charter state.

## Sign dialogs and landing against D41 / D49 / D53

- **D41 (Mac: Touch ID per human signature, showing action and hash).** Every human signature goes through core's
  dialogs (`SignDialog`, `SignPrompt`, `SignConfirm`, `DecisionSignPrompt`, the grant dialogs): what is signed in
  words, then "Touch ID" (simulated 600 ms), one signature per act; signed events carry `presence: 'touchid'`. The
  hash and covers are in a folded "Details" in the dashboard; the OS Touch ID prompt is where D41 shows action and
  hash, which the real host must pass to it. Agents never reach these dialogs (`human_only`).
- **D49 (iPhone: no per-signature Face ID).** The dashboard does not model phone signing; Relay & devices lists the
  iPhone with its scopes, and the Today re-login row marks phone prompts "P4 · Preview". Nothing contradicts D49.
- **D53 (landing).** The land addon binds approval, checks and merge to one candidate SHA; a clean rebase keeps the
  approval (re-checked), a conflict resolution voids it — through core (`store.landingResolved`: core checks the
  failed attempt and the resolution records and writes the reason), never by the addon. `main` is allowed for owners (D33, owner decision 11 Oct), with aliases normalised.
  Core signs `enqueue_main` covering ticket, target `main` and commit; other roles receive 403. A mandate can never do
  more than its owner. Decided (10 Oct): the verdict signs the branch head (`source_sha`), the land worker uses
  that commit, new commits void the verdict, and the opt-in `code` gate (when on) signs the same commit.
- **Third-party HTML (security review #1).** Addon frame nodes, agent HTML artifacts and one-off widget pages are
  drawn **inert**: core's sanitizer (`addon-ui/frameSanitize.ts`) first removes everything that could navigate the
  frame or load something (script, meta refresh, base, link, iframe/object/embed, forms (unwrapped), SVG animation,
  every `href` except `#anchors`, every `src` except data: images, `target`, `ping`, `formaction`, `srcset`, event
  handlers, style `url()`/`@import`), then the frame gets `sandbox=""` (no scripts) and a no-script CSP. Its height is
  fixed (the node's), the content scrolls inside, the person can drag it taller. Only core's own pinned widget
  templates (`api/widgetTemplates.ts`, data as inert JSON) run scripts (`sandbox="allow-scripts"`) and fit their
  height. Removing a frame after a second load is no longer relied on. Agent HTML that relies on scripts (script
  elements, event handlers, `javascript:` URLs: `usesScripts`) is not drawn as an empty frame: core shows "This preview
  used scripts, which orch no longer runs for agent HTML — showing the source." with the source (`ScriptedPreview`;
  the artifact viewer offers the static preview from there). **A real host that wants to show active
  third-party HTML (scripts) must render it in an environment that blocks every outbound request and navigation
  before dispatch** (not in the dashboard's page).
- **Addon contributions are bounded before core walks them (security review #9):** at most 64 levels, 20,000 values
  and 2 MB of strings (`nodeBudgetProblem`, iterative, counting every value before it is queued); the node is checked
  again after binding resolution (a `$ref` can bind any state), and interpolation never stringifies an object; over
  budget, only that contribution shows "could not be shown".
- **D54.** The relay is API only; its one allowed page is the static, script-free fallback for the pairing link (`/pair`). The dashboard draws no relay-hosted page. Decided (10 Oct): strict D54 — Drop is orch-only, no outsider download page; revisit later.

## Routes (permanent URLs)

The app owns real paths (browser history). Every page, ticket, settings tab and addon page has an address that opens
the same view after a reload or pasted into a new tab. **The host serves `index.html` for every app path below**
(SPA fallback: `npm run dev` and `npm run preview` do this already) and the built assets from `/assets/` (Vite
`base: '/'`, so a reload on a deep path still finds them).

| Address | View |
|---|---|
| `/w/<PREFIX>` | Today of that workspace (Today is per workspace) |
| `/w/<PREFIX>/board?view=list&mine=true&type=…&label=…&person=…&epic=…&q=…` | Board, its view and filters |
| `/w/<PREFIX>/tickets?q=…&status=…&type=…&priority=…&person=…&needs=…&label=…&sort=…` | Tickets list and filters (saved views are these params) |
| `/w/<PREFIX>/tickets/new` | New ticket page |
| `/w/<PREFIX>/ticket/<KEY>?tab=acceptance\|changes\|questions\|artifacts\|history\|raw` | A ticket and its tab (Overview without `tab`); `#question-<id>` opens that question. The key names the workspace: `/ticket/<KEY>` and `/w/<OTHER>/ticket/<KEY>` redirect (replace) here. |
| `/w/<PREFIX>/artifacts?view=list\|grid&a=<KEY>.<sha256[:12]>` | Artifacts, layout and the shown artifact |
| `/w/<PREFIX>/agents?tab=mandates` | Agents (Sessions without `tab`); `mandates` is the **preview** tab (not part of the contract) |
| `/w/<PREFIX>/settings/<tab>` | Settings tab (general, members, gates, relay, addons, skills, connections) |
| `/w/<PREFIX>/settings/addon/<name>` | Settings > Addons with that addon's row open |
| `/w/<PREFIX>/addon/<name>/<page>?tab.<node>=<tab>` | An addon page, and the open tab of core's tabs node `<node>` |

- **Mechanics.** The route tree keeps short in-app paths (`/board`, `/settings/$tab`); a router rewrite
  (`src/app/urls.ts`) strips `/w/<PREFIX>` on the way in and adds the current workspace on the way out, so every
  `<Link>` gets the workspace without naming it. On a `/w/…` address the address decides the workspace
  (`WorkspaceProvider`); on a ticket the ticket's home workspace (its key's prefix, never the remembered one) becomes current and is in the address. The last workspace is remembered
  (localStorage) for addresses without one.
- **Old addresses keep working:** `/`, `/board`, `/settings/…` etc. are replaced by the current workspace's address;
  `/settings` → `/settings/general`; `/settings/addons/<name>` → `/settings/addon/<name>`; `/ticket/<KEY>` →
  `/w/<KEY's PREFIX>/ticket/<KEY>` (keeping `?tab=` and `#question-…`); `/w/<OTHER>/ticket/<KEY>` → the key's own workspace.
- **Not found:** an unknown `/w/<PREFIX>` shows "No workspace <PREFIX>" (the address stays); an unknown ticket,
  a restricted one, an addon that is off or a missing addon page show their existing states. An unknown settings tab
  still goes to General (in the address's workspace: the router's redirects keep `/w/<PREFIX>`). Prefixes match
  in any case (`/w/demo` → `/w/DEMO`); `/w/` is Today.
- **View state** is in search params, each validated on its own (`src/app/search.ts`, zod): an invalid value is
  dropped and the default applies. Dialogs, signing prompts, the terminal dock and the demo dataset are not in the URL.
- **Privacy:** addresses carry keys and ids only (ticket keys, workspace prefixes, addon names, artifact
  `<ticket>.<hash prefix>`), and the search text a person typed; never titles, tokens or signed values.
- **Copy link** (ticket header, Settings, addon pages, ⌘K "Copy link to this page") copies the address bar
  exactly, as origin + address (`useCopyLink`, `src/app/copyLink.ts`).
- **Tests** pass an initial path to `createAppRouter(path)` (memory history; in-app or `/w/…` paths both work);
  `renderApp` returns `address()` (the address bar as the person sees it).

## Page loading (G4): what the host maps

- **Route loaders = the host's GET endpoints.** Each page route's loader (`src/app/routeData.ts`) warms the queries
  the page needs, through the same `api.*` calls the components use. With a real host they become real requests;
  nothing else changes. Keep each page's first screen to one or two round trips (the shell's `me`, workspaces and
  addons, then the page's own data in parallel), or pages wait for the skeleton more often.
- **Latency the UI is tuned for:** under 200 ms the old page stays and the new one replaces it in one step; past
  200 ms the page's skeleton shows (at least 300 ms); a loader holds a page at most 1.5 s, then the page shows with
  its own placeholders. A local daemon answering in ~50–150 ms gives the one-step case nearly always. The mock uses
  120 ms per burst; `?jitter=1` (120–300 ms per request) and `?latency=<ms>` test slower hosts.
- **Shell data before the first paint:** the first screen waits for `me`, the workspaces, the addons and the demo
  dataset (the demo one only exists in the mock). A host should answer these fast; they are the cold-start cost.

## How to run, test and preview

```
cd packages/addons/dashboard
npm install
npm run dev                      # dev server (the session uses 5180 live and 5181 as a stable snapshot)
npx vitest run <path>            # targeted tests after each change
npx vitest run --maxWorkers=3    # full suite, ~1,790 tests, a few minutes; once per phase (local only, not in CI)
npm run typecheck
npm run build                    # dist/
npm run layout:guard             # 13" notebook check (below)
```

**Layout guard (`npm run layout:guard`, `scripts/layout-guard.mjs`).** The owner's minimum: every page works on a 13"
notebook in full screen with the terminal docked on the right, without sideways scrolling. jsdom cannot lay out, so
the guard drives headless Chrome over the DevTools protocol (plain node, no Playwright) against a running dev build
(`npx vite --port 5201 --strictPort --host 127.0.0.1`, or `--url <base>`). It moves between routes through
`window.__orchRouter` (dev builds only; it pushes in-app paths, which the app turns into `/w/DEMO/…` addresses) and checks every core page, every addon page (also under "More addons"),
every Settings tab and addon drawer, every tab of DEMO-0043, `/tickets/new`, the New ticket overlay and the Review
tour sheet, at 1440×900 and 1470×956, right dock at min / default / max width, sidebar wide and rail, Normal and Busy
day. It fails (exit 1) on document or element overflow outside the allowed scrollers (terminal, `pre`/`code`, tab
rows, `data-scroll-x`), on a cut-off button/link/tab, or when a step did not open; silent clipping is a warning.
Options: `--quick` (one configuration), `--docks min,max`, `--dataset busy`, `--only <regex>`, `--shots <dir>`,
`--selftest` (plants overflow and breaks steps on purpose to prove it fails). Last run: 0 failing.

**Working rules** (the owner's): Opus manager, Sonnet implementers, Opus review for security-relevant work. Targeted
tests after each small change; full suite, typecheck and build at the end of a group. Commit only `packages/addons/dashboard`
paths; never commit `.design-drafts/`. No realistic-looking secrets in seeds (GitHub push protection).

**Preview for the owner.** Local: http://127.0.0.1:5180/ (live) and http://127.0.0.1:5181/ (stable snapshot). The
hosted preview (https://claude.ai/artifact/QvyFVD1JtFPyb3MegNXjgT) is **frozen at 98151971**: since G2 the build
uses `base: '/'` and lazy chunks import `/assets/…`, which the sandboxed viewer does not serve, so the old publish
steps (`npm run build`, `python3 scripts/build-preview.py dist <folder>`, publish to the artifact URL) are obsolete
for G2+ builds. `build-preview.py` still runs, but its output does not load; it is kept only for reference.

## Known gaps and notes

- The hosted claude.ai preview is no longer a target for routing (owner, 2026-10-10): with `base: '/'` the built
  chunks load from `/assets/`, which the sandboxed viewer does not serve, so a preview published from this build is
  expected not to load. Local dev, `npm run preview` and a real host are the targets.
- `confirmed` (core's confirm flag on spawn/sign/decision actions) is a plain boolean in the mock. A real host needs
  a server-issued, single-use confirmation bound to the person, the action and the arguments.
- The start dialog's facts come from core's `agents/launch` preview; a real host renders them from its own resolver.
- The land worker's real push is a fast-forward with a lease on the target SHA, and at push
  time the real worker must verify the core signature binds the ticket, target `main` and commit,
  and that its signer was an owner and still has owner authority (D33, 11 Oct; supersedes N9 refusal).
- Addon settings drawer: a save by someone else while you edit resets your edits (needs an "Updated elsewhere" design).
- The simulated Claude welcome box clips in a very narrow terminal (allowed: terminal content).
- **Mandates are a preview only (M1), not part of the contract.** Agents → Mandates, the shell's mandate banner,
  Today's "Decided for you" and Demo data → "Preview: mandates" show how the wide mandate (owner decision 10 Oct
  evening, `docs/concept-mandates.md`: whole workspace minus the always-human areas and protected paths, up to 30 days,
  renewable, never another mandate, may start full runs that Deliver) would look. Nothing signs; the preview endpoint, its types (`src/api/mandatesPreview.ts`)
  and words are provisional. Off by default; a host ships none of it until core specifies mandates. The real build
  needs the four prerequisites (P2 custody, isolated execution, host-minted identities and checker, typed effects with
  durable counters and a time guard) before any of it can be enabled.

## Next

1. **Owner review** with [REVIEW.md](REVIEW.md) and the in-app Review tour (the first round's questions are decided).
2. **Next PR:** push `feat/dashboard-mockup` (`origin/develop` is already merged in), open a PR against `develop`,
   ping the orch v2 build session (orch-4e) when it is open. Merge only on green CI. The dashboard's vitest suite and
   `npm run layout:guard` run locally only (CI does not run them): run both before asking for the merge.
3. **Backend swap**, once the P2 host exposes the operation registry over its socket:
   - implement the operations named in "Backend contract", and the proposals the owner accepts;
   - point `client.ts` at a transport over the host (`createFetchTransport` or a socket transport with the same
     `Transport` shape); remove `/api/dev/*` and the review tour;
   - settle event names with issue #338 and rename the provisional ones in the mock and `describeEvent`;
   - move the rules the mock enforces (permissions, visibility, gate policy, addon roles, refusal codes) into the
     host; the UI keeps them only as convenience.

## Repos addon preview (U3)

The declared repo list is the workspace's `settings.repos` (`Workspace.repos`, folded from `settings.changed`
`set.repos`, ticket format §2/§5.4.2); the addon keeps only observations, jobs, its log, settings and a draft.
`remote`/`default_branch` per entry are a proposed format amendment. Seeds: `fixtures/workspaces.json` (`repos`,
`root_folder`); Busy day adds `busy/repos.ts` (every generated `links.repos` name is declared). Proposal:
[repos-addon-proposal.md](docs/repos-addon-proposal.md).

| Endpoint / action | Minimum role | Behavior |
|---|---|---|
| `POST /api/workspaces/:ws/settings` `{op: 'repos', set}` | owner (person) | Core's `settings.changed`; bad name/path/remote 400; same path 409 `settings.repos_same_path`; `store.changeRepos` is the one writer. |
| `GET …/addons/repos/state` | viewer | Structure, Checks, Activity, Glance, settings, ticket panel. `moving: true` while a clone runs: core's shared query re-reads every 1 s. Buttons only for the roles that may use them. |
| `POST …/repos/actions/prepare_add`, `add`, `adopt` | **owner** | Host-validated (`src/api/repos.ts`, shared with the UI); add/adopt signed; write `settings.changed` through `changeRepos`. |
| `POST …/repos/actions/remove`, `remove_anyway` | **owner** | Destructive / options confirm; 409 `repos.linked` with the count of open tickets the owner can see; `settings.changed` with `null`; the disk is untouched. |
| `POST …/repos/actions/clone`, `clone_all`, `clone_attention` | maintainer | Signed remote, target folder and `clone_as` (the git-login connection); stale plan or identity 409 `repos.changed`; no remote 409 `repos.no_remote`; no login 409 `repos.no_login`. |
| `POST …/repos/actions/check`, `fetch`, `fetch_all` | member | Re-read / refresh tracking; never pull or discard changes. |
| `POST …/repos/actions/open_terminal` | member | Repos and Terminals pty grants; dock shell with `cd -- '<path>'` typed, not run. |
| `POST …/repos/actions/save_settings` | maintainer | Interval, fetch-on-check, git-login connection (gh/glab/git CLI logins only). |
| `GET /api/workspaces/:ws/tickets?repo=:name` | viewer | Exact `links.repos` filter; visibility enforced. |

Round 2: `settings.changed` is refused by `store.appendWs` unless an owner person signs it, and not folded otherwise
(replay); `settings`, `branch`, `invalid`, `terminal`, `visibility` are reserved addon names. A queued clone runs only
its signed spec; a later declaration change cancels it. The shared addon-state query also honours a state's
`nextRefreshMs` (bounded 30 s – 1 h; `addonRefresh` in queries.ts), which Repos sets for its scheduled check.

Host contract (real host): clone runs `git clone -- <remote> <path>` (with `--`), never through a shell, as the
connection's own CLI login (D56 A); orch stores no git credentials. Remotes are refused with any userinfo, query,
fragment, non-ASCII character or a part starting with `-`.

Links: a repo row is `/w/DEMO/addon/repos/repos?tab.repos=structure&row=web-portal` (core opens the row whose key is
`row`, on any addon page); the ticket list by repo is `/w/DEMO/tickets?repo=web-portal`.

## P2 server (M0)

Implemented 2026-10-11. The addon-local Python package lives in `server/` (including pyproject
and uv.lock), launched with `uv run orch-dashboard open` or `python -m orch_dashboard open`.
See [server/README.md](server/README.md) for the complete dev stub + server + Vite instructions,
auth rules, route table and provisional stream A integration assumptions.

Production builds use `src/main.prod.tsx` and `src/production/Shell.tsx`: dark sidebar,
UUID workspace switcher, person identity, minimal Today/needs and paged tickets. Navigation
comes from `/api/session` per-workspace features. All other mock pages/actions are absent.
The mock app stays the default Vite/test/layout entry; its singleton moved to `api/mockApi.ts`
behind the compatibility `api/client.ts`. Fetch transport has no runtime mock dependency.
The build rejects mock modules and copies assets into the wheel's package tree.

M0 HTTP reads map to fixed host calls: workspaces → `host.list`, `addon.list`, `workspace.show`,
`member.list`, `needs`; identity/me → `whoami`, roster and host status; today → `needs`;
tickets → `ticket.query`. Browser input cannot choose operations, paths or principals.
Adapters use existing UI type projections with missing fields explicitly unavailable.
No signing, factor handling, mutation endpoint or browser subscription is present.

The server checks UID and workspace/person pins from the private host registry, not config.json.
`serve_http` is a server-side addon policy check, not a host security boundary. Control discovery
and registry container shapes need stream A confirmation (R11). Reserved P1 read wrappers are
listed in `devhost/reserved.py`, excluded from the wheel, gated by ORCH_DEV and `.orch-dev`.
The polling stub invalidates subscriptions on change; real journal replay and browser live
updates remain M1. Op/version capability checks hide unavailable features.

CI now runs dashboard Node checks and Python 3.14 checks in `.github/workflows/dashboard.yml`.
No core files or core CI were changed. Real-port/browser/layout and real-host S1/S2 verification
remain acceptance work in the dev-kit; none of #304–#306 is closed by these stub tests.
## Owner answers applied — 11 Oct morning (U8)

These decisions supersede earlier open questions and defaults in this handover.
Mandates remain non-signing preview only: code-review approval, first linked-workspace send,
public publish and restore/purge are allowed. Landing on main is allowed for owners and their mandates (D33, 11 Oct);
a mandate can never do more than its owner. Another mandate, protected paths, settings, addons, members/roles,
devices, relay pairing and secrets/connections stay human. No total renewal ceiling. “Weekly check-in due <day>”
and “Still OK” preview confirmation; after 3 unconfirmed days the mandate pauses, not revokes.
A paused mandate can be renewed only after “Still OK” checks it in. The mock seeds the due date and pauses
on read; a real host must pause on a timer, independently of reads. Provisional events:
`mandate.paused {reason: check_in_overdue}` and `mandate.check_in {check_in_due}`.

Factory holds: integer 0–1440 minutes, default 15; form 0 / 15 / 30 / 60 / 240 / 1440.
A new run with zero hold signs “No hold: delivers as soon as its checks pass”; re-delivery signs “Delivers as soon as you sign”. Only owners (and future eligible mandates) sign Deliver.
Children reserved at admission may finish at the child budget limit; new children cannot enter.
Phone Stop is a push notice + Face ID Stop, implemented by orch-mobile (dashboard has no mobile implementation).

| Endpoint / action | Operation and enforcement |
|---|---|
| Preview mandates, `check_in` | Owner-only unsigned preview; advance due date a week and resume a check-in pause; never a signing endpoint. |
| Factory `prepare_redelivery` | Owner chooses a stopped run and new hold; host issues an unsigned single-use request. |
| Factory `redeliver` | Core signs run, destination, hold, request and delivery revision; host checks owner, stopped state, exact draft, single use and charter. Reuses all completed work. |

Provisional addon event `factory.redelivery_requested {request, run, deliver_means, hold_minutes, revision}`,
actor `addon:factory`. Core owns `addon.action_signed`; zero delivers immediately, positive starts a new hold.
Each new hold uses `factory.hold:<run>:<revision>` (initial hold keeps `factory.hold:<run>`); the revision is a signed term, so an old Stop cannot answer a later hold.
An active hold cannot be shortened; Stop first, then a fresh delivery signature.

Repos allows literal `ssh://git@host/path`, never another user/password or leading-dash host/path.
No primary flag. `remote` + `default_branch`: **proposed for the format amendment (owner agreed 11 Oct)**.
Last gate: **Verify** (core-owned; the ticket body section remains Verification). Addon log tabs: **Activity**;
ticket History stays History. Ellipsis marks a dialog asking for input, never a plain confirmation or signing prompt.
Factory permits sit in a reserved folded “n permits waiting · Review” row; Today retains the needs-you decisions.

### D33 follow-up — owner landing on main (11 Oct)

Owners can add `main` to Allowed targets. `refs/heads/main`, `origin/main`, remote refs and case variants
resolve to `main`. Enqueue explicitly names an allowed target; the default is the first non-main target
(or main when it is the only target). The ticket panel offers each allowed target, preserving other roles. `enqueue_main` is an owner-only
core `confirm: 'sign'` action; Covers show the ticket, target and approved commit. The ordinary `enqueue`
refuses main without that signed path. Stale target/commit confirmation returns 409 `land.changed`.
The simulated worker reads core's `addon.action_signed` record and rechecks owner authority at merge
time; missing signature or lost authority fails the attempt without advancing the target head.
For a red-check retry on main, take it off the queue and sign a new landing; the ordinary rerun decision
cannot bypass signing. The real worker must enforce the same signature and owner checks at push time.
Mandates remain a non-functional preview: an owner's mandate may land on main but can never exceed
its owner, and protected paths still need a person.


### D33 fix round — ref and signature boundaries (11 Oct)

Host, UI and worker share `src/api/land.ts`: repeatedly strip local and remote ref prefixes and configured
remote names, case-fold, then refuse residual ref namespaces and invalid git branch names. The mock's
`settings.remote_names` represents configured git remote names (origin/upstream are always recognised);
the real host must obtain these names from git configuration, not from request arguments. Whitespace,
control characters and Unicode lookalikes are refused; comma-separated settings permit delimiter spaces.
At push time the real worker must fully resolve the remote target to `refs/heads/<name>` and compare that
resolved ref against the remote's protected/default branch before applying the owner rule. A spelling-only
check is insufficient. Recheck authority and the bound commit immediately before that push.

Each enqueue_main entry stores the exact core `addon.action_signed` event ID, bound to its unique
`land.queued` event ID. The worker checks that exact record and binding and consumes it on merge;
a new entry requires a new signature even for the same ticket and commit. Rebuilding a candidate after
target movement keeps the same entry. A real host must consume the signed confirmation once atomically
with entry creation and persist the entry binding and merge consumption across crashes. Land state is v2;
old mock state reseeds, so old unbound entries cannot gain authority.

For queues shared across workspaces, the signer must still be an owner of the **ticket's workspace**,
and that workspace must declare in `settings.repos` the repository whose canonical remote the queue uses.
An owner of a different workspace sharing the same remote has no authority over this ticket's landing.
Both conditions are enforced at enqueue and rechecked at merge/push time. The demo repository declarations
and landing remotes agree; no authority is inferred solely from sharing a remote.

When mandates become functional, a `via: mandate` signature for main is valid only if its issuer is still
an owner of the ticket's workspace at merge time, with the same declared-repository requirement.
The current mock accepts only person signatures.

## P2 server (M1 live updates)

Implemented 2026-10-11 on contract v1.3.1. Supersedes the M0 live-update and control-discovery
limitations above. `server/src/orch_dashboard/live.py` owns one subscription per active UUID,
snapshot → subscribe, item-id deduplication, reset → snapshot/reopen, and browser fan-out.
`GET /api/events?ws=<uuid>` requires bearer auth and accepts per-tab `Last-Event-ID`;
`GET /api/w/<uuid>/head` supports fallback polling. No server replay buffer. Ready gates the
production shell's first workspace fetch; gap/reset invalidates workspace queries.

Visibility and membership are checked again when a browser drains a batch. Gone cancels and
purges ticket details and removes visible rows immediately; revoked purges workspace state.
The 256-message queue and 16-stream session cap bound fan-out. Heartbeats run every 15 seconds;
logout/expiry also cancels a blocked ASGI send. Mock transport is unchanged.

The dev-only stub emits metadata from P1 log polling and supports in-memory scoped cursor
replay, gone, sub.close and reset/overflow. It stays read-only, without human operations.
The client supports v1.3.1 frame bounds, control welcome, host.list person mapping and newer
human argument shapes. See [server/README.md](server/README.md) for precise stub contract gaps.

Carry-overs N1–N3: prefer the private XDG runtime redirect directory; `open --print-url`
requires a terminal and labels the link single-use/30 s; startup sweeps stale redirects;
run-directory guidance requires a short real directory, never a symlink alias.

No port listeners were started for implementation tests. Socketpair/P1, in-memory ASGI and
Vitest cover lifecycle, reconnect, resync, visibility, gone, revocation and bounds. Real-host
S3/S4, the real-browser layout guard and cross-browser network behavior remain acceptance
work. Full ticket views/hand-off are later milestones; their future consumers must use
UUID-keyed ticket factories and the live hook's close-view callback.

## P2 server (M2 reads)

Implemented 2026-10-11 on contract v1.3.1, then adapted to v1.4 (below), on top of the final M1 (#380). #305 reads only: nothing signs or mutates,
and every control that would is not drawn (`/api/session` lists no `actions` feature until M3).

**One app.** The production build now draws the mockup's own shell and pages (`src/production/Shell.tsx`
mounts `createAppRouter`), not the M0 mini shell. `vite build` aliases `@/api/client` to
`api/client.prod.ts`, so every page's `api` is `api/prodApi.ts` (the same object, behind the fetch
transport); the module-graph check still fails the build if a mock module enters. The mock entry, tests
and the layout guard keep the in-process mock (`api/features.ts` lists everything for them).
`hasFeature(name, ws)` gates navigation, routes (`serves(...)` in `router.tsx`) and a few panels;
`can(role, perm)` is also false for every role without the `actions` feature, so each action that was
already behind a permission disappears. Today still lists what needs you (rows say "Answer in your
terminal"); a viewer role still gets the read-only layout.

**Routes** (closed list in `server/README.md`): `/today`, `/tickets`, `/board`, `/tickets/<key>`,
`.../events`, `.../changes`, `.../artifacts/<name>`, each with a pydantic input model that forbids extras,
one fixed host op behind it (`needs`+`ticket.query`, `ticket.query`, `ticket.get`, `ticket.get`, `diff`,
`artifact.get`) and an adapter in `server/src/orch_dashboard/adapters/`. Queries are keyed by workspace
UUID: `['ticket', ws, key]`, `['ticket-events', ws, key, seq]`, `['changes', ws, key, head]`;
`queries.ticket(ws, key)` replaced `queries.ticket(key)`, and `api.getTicket/getEvents/getChanges` take
the workspace as a last argument (the mock ignores it).

**Adapters** unwrap `result.data`, spell `in_progress` as `in-progress`, and leave empty what the host does
not serve; `TicketDocument.unavailable` names those fields and `OrchEvent.summary` carries the host's
one-line event text (History shows it instead of guessing from payload fields). Golden JSON of the real
adapters is written by pytest (`UPDATE_GOLDEN=1`) into `src/test/fixtures/server-m2/`, and the browser
tests render the same files.

**Host contract v1.4 (adapted 2026-10-11, same milestone).** The branch now targets §0 v1.4 (`read_ext/1`,
`activity/1`). The server reads the caps from `welcome.caps` (`HostLink.has`) and lists `read_ext` and `activity` as
per-workspace features. **A field a host without `read_ext/1` leaves out is unknown, never zero:** row `progress`,
`restricted` and `blocking_questions` are `null` (no bar, no lock, no marker), the document says `read_ext: false` and
names what it lacks in `unavailable`. What v1.4 now provides, and how the UI uses it:

| v1.3.1 gap | v1.4 | In the UI |
|---|---|---|
| Row counts, `restricted`, blocking questions | `tasks`, `criteria`, `restricted`, `blocking_questions` | Cards: progress bar, lock, blocked marker (`skipped` has no slot) |
| `needs` question text, `since` | `text` (Snip), `since` (absent = unknown), `status`, `payload` | Today rows with age only when `since` is known; question text from the need; "shortened" flag for a cut Snip |
| `ticket.get` option labels, `asked_*`, answer | `Question.options/answer/asked_*/to_ref/why/recommended` | Questions tab; `stale` state; no invented "via dashboard / Touch ID" |
| Task receipts and leases | `Task` (`leased_by`, `receipt`) | "doing" = `leased_by`; lease shows the session only |
| Gate approvals and policy | `GateView.decisions/policy/eligible/waiting/blocked/count` | Approvals, presence from `auth` (passphrase, secure-enclave), policy text; `eligible` lists people who can see the ticket |
| Verdict | `verdict{outcome, source_sha:[Source], current, voided}` | `result` from `outcome`; a list of commits; "from before the latest change" when not current |
| Artifact manifest | `artifacts[]` (`digest` with `sha256:`, `by` Actor, `bytes` null for addon artifacts) | Prefix stripped, `by` as a name, unknown size drawn as "–" |
| Claim agent/`for` | `ClaimFull` (`live`, `lapsed`) | Turn "claim has lapsed" when `live` is false; expiry still unknown |
| Closed tickets | §0.8.4: plain read, `closed{…}`, `status:["closed"]` | The 409 `ticket.closed` is gone; the page draws a Closed notice (`duplicate_of` as plain text); Tickets has a Closed filter; Today flags needs of closed tickets |
| `needs` kinds `no_eligible`, `acknowledge`, `addon` | `payload.cause` (`no_token` or `no_visible_person`), rule text | New notice row on Today (it used to be drawn as a verdict row) |
| Workspace-wide recent activity | `activity.list` (`scope:"tickets"`), snapshot paging | Today's "Recently"; follows `next_page` across short pages, dedupes by `id`; `jpos`, `ws_seq`, `log` and `uid` are never a field in what the browser gets (`jpos` can still appear inside opaque ids and cursors, e.g. `<jpos>.e`) |
| Event payloads | `LogItem.data` (nested, closed per type) | Flattened beside the core keys; `<field>_cut` becomes `truncated`, shown as "(shortened by the host)" |
| Section history | `sections` (hashes only) | History › Changes lists who, when and the hash; no diff, ever |

**Still not served (asks for v1.5, collected here):**

| Gap | Effect in the UI today |
|---|---|
| Workspace policy and settings read (`grant_hours`, `claim_ttl_min`, `lease_ttl_min`, `repos`, default gate policy) | `Claim.expires` is empty; settings stay unavailable (M4) |
| Full text of comments, change requests, verdicts and closing reasons (Snips stop at 1 KiB) | Shown cut with a flag; no "more" button because no op returns the rest |
| Question hash and the full gate hash for the signed dialogs (only `hash12`) | M3 must get them from `human.prepare`; confirm it binds them |
| `via: 'factory_charter'` approvals, grant `revoked.by`, grant session ids, answer `via`/presence | Not expressed; nothing is made up |
| Branch commits (`TicketBranch.commits`, base, per-commit data); `diff` one head/base per repo | Not stored; the Changes tab shows the diff only |
| Subscription items carry no actor, summary or data | The feed refetches its first page on an invalidation |
| Search snippets (`TicketSummary.match`), artifact `url`/`preview` for link and inline artifacts, per-status counts, workspace artifact browser, agent activity and refusals | Empty or absent |

**Stub boundaries (dev only):** the stub serves the v1.4 read shapes (`ORCH_DEV_READ_EXT=0` turns `read_ext/1` and `activity/1` off to test the unknown case); `ticket.query` and `activity.list` snapshots live in memory (32, 10 minutes, bound to the person and the arguments); the sort of
`priority` puts `urgent` first for `desc`; `epic` is the parent when the parent is an epic; `diff` runs the
scrubbed git helpers of core against the workspace's `settings.repos` and refuses unless the branch head
equals the head the host observed. Nothing here is a real-host guarantee.

Live updates: unchanged from M1, now mounted in the app shell. `ready` refetches the workspace's queries
(this replaces gating the first fetch on `ready`: the refetch starts after the subscription, so no change
falls in between). A `gone` ticket closes its open page (toast, back to the board) and removes the cached
document, log and diff. Same-prefix workspaces are not told apart by the address yet (the M0 mini shell's
`?ws=` suffix is gone with it); that belongs to the switcher milestone.

**M2 fix round (review-M2).** The epic children and refusals queries are keyed by workspace
(`['ticket-children', ws, key]`, `['ticket-refusals', ws, key, seq]`), so `gone`, `revoked` and `ready` reach them.
`gone` closes only the open ticket page whose key was collected for that uid (`close(uid, keys)`). `parent` and
`blocked_by` are plain text unless the key is a visible row in the cached tickets list or board (contract 0.5), and
nothing preloads them. After a `revoked` the shell shows a persistent "Workspace access is no longer available."
toast (the caches are purged as before). `artifact.get` is gated on `diff/1`, an empty non-final chunk is an error
(bounded loop), and a null `priority` stays null (no marker, "-" in the rail). The build fails when sonner is bundled
but its transform did not match exactly once. **Known, harmless:** Radix's scroll lock (`react-style-singleton`) and
xterm's DOM renderer still create `<style>` elements; the CSP (`style-src 'self'`) blocks them, the console logs the
violation, Radix dialogs do not lock body scroll and the terminal keeps its own styling. The CSP is not loosened.

## P2 server (M3 actions)

Signed actions through the terminal hand-off (#305). **The dashboard never signs, holds no factor and never calls
`human.submit`.** The person signs with `orch sign CODE` (H5b); the dashboard prepares, shows, follows and cancels.

| Route | Host op | Notes |
| --- | --- | --- |
| `POST /api/w/<uuid>/handoff` | `human.prepare {op, args, handoff:true}` | body: `idem` (`a_` + 26 Crockford chars, client-made, one per click) and a typed action (`approve`, `request_changes`, `verdict`, `answer`, `comment` -> `log`, `label` -> `set labels=`, `close`, `reopen`, `new`, `details` -> `set`, `section` -> `section.set`). `extra=forbid`; `new` carries title and type only; the follow-ups are `details` (priority, size, labels, parent: one `set`) and `section` (one `section.set` per written section; id from the closed list, text 1..65536). `move`, `ask`, settings: 400. Requests belong to the session token that prepared them (other token: 404). The same idem from the same token = the recorded answer; two tabs = two requests; 8 open per session (`quota.host`). After a lost prepare answer the retry first cancels the person's open request for that action created since the lost call |
| `GET /api/w/<uuid>/handoff/<request_id>` | `human.pending {request_id}` | `{state, event_ids, seq, error?}`; owner only |
| `DELETE /api/w/<uuid>/handoff/<request_id>` | `human.cancel` | idempotent; logout cancels the session's open requests |

The answer is a `HandoffRequest {id, code, expires_at, state, op, key, fingerprint, review_digest, gate_hash, material, events}`.
The server recomputes `review_digest` and the fingerprint (`sha256(cj({review_digest, events without sig, actor.device,
auth}))[:12]`, host contract 0.8.1) and refuses (`internal`) an answer that disagrees; the browser recomputes both again
and shows nothing otherwise. The preview is only `material`. Feature `actions` is listed when the host serves
`human.prepare/get/pending/cancel`; `move` is never listed (H6b), so `can(role, 'ticket.move')` is off on a real host.
Gate `material.policy` and section `texts` are rendered when present (v1.5, optional).

Dev stub: `devhost/humanops.py` (v1.4 `human.*` on the P1 store), `devhost/sign.py` (`python -m devhost sign CODE`).
Both are dev only, never shipped, and excluded from the wheel (tested). Stub limits: `new` carries title and type only
(P1 `ticket.created` has no other fields), no `observe` of git for code gates, requests are in memory.
Known gaps: the fingerprint will also cover the op once H5a lands (the client check then needs that formula); the uid
fragment is the last 8 characters of the log id in both the panel and the stub signer (confirm with stream A).

M3 fix round 1: the server checks the host's answer is the op and ticket asked for, `state` is `prepared`, `expires_at` is
well-formed and within 15 minutes, and each `material.review` block equals its event minus the envelope (the browser checks
the same). Stub signer checks left out (no data in the stub): `material.policy` vs `policy_hash`, git state for a code
gate, the op in the fingerprint. (`human.prepare` once ignored `opts.idem`; since stream A #402 it honours it, see "M3 fix: idem retry" below.)

H5a answers (stream A, #391) applied: the fingerprint covers the op (`sha256(cj({op, review_digest, events:[V(e)]}))[:12]`;
vectors `log` d8de03967462, `set` 37cf21ef169c are tests in the stub, server and browser); `gate_gen` leaves the review payload
on the three decision types only; the confirmation word is `KEY/FRAG` (last 8 characters of the log's uid) and the panel and the
stub signer print the same five header lines with the key labelled "ticket key per host, not proven"; a refused submit leaves the
request `bound` with `error:{code}` in `human.pending`. `human.pending` rows carry no origin; `human.get` does.
Not yet from H5a: gate `material.texts` is shown only if present; the signer's op-specific `ticket.updated` checks are not in the stub signer.

M3 complete details (this round): after a signed `new` the page offers "Complete details" (`new-ticket/CompleteDetails.tsx`), one
hand-off per step, in order: one `details` (`set`: priority, size, labels, parent epic) and one `section` per written section
(`section.set`). Host contract 0.8.1 item 3 and "Person versions of agent ops" name `set` and `section.set` as person ops, so
the stub now serves both (`parent=KEY|none` validated against visible tickets and not the ticket itself; `section.set` appends
with `body=` so the P1 store checks the text against the signed hash; the stub signer checks that `section.set` changes one
section and no field, `set` fields only, and that `base_rev` names exactly the touched paths). The list shown is the person's
own input; the preview inside the dialog is the host's material only. Cancelling any step ends the offer (the rest stays
unset). Still refused up front, with the reason in the form: restricted visibility, people, due date, acceptance criteria (the
host has no person op that sets them). A spike's "verification" text travels as the host's `findings` section.

H5b alignment (stream A, #398): the stub's `human.pending {code}` now matches contract 0.8.1 item 2 and 3: a code resolves only while the
request is `prepared` or `bound`; a lookup that finds nothing (including the code of a submitted, cancelled or expired request) is a
failed lookup; 5 failed lookups in 60 s **per connection** answer `rate.limited` (retryable, `retry_after_ms`), the right code
included; another connection and `{request_id}` lookups are not counted (the stub has one person and one workspace, so the
same-principal/person/workspace rules hold trivially). The dashboard server follows a request by `request_id` only and never sends a
code (`human.pending {}` lists the session's own open requests; tested). M3 offers no artifact hand-off (`artifact.add/replace` would
need `orch sign CODE --file NAME=PATH`).

Real `orch sign` against the stub (smoke, 12 Oct): it works. With the same disposable `ORCH_STATE_DIR`/`ORCH_DEV=1` setup, core's
`python -m orch sign CODE` (develop, H5b) found the stub's registry entry and socket, resolved the code, bound the device, ran its checks
and signed `new`, `set` and `section.set` requests and a `log`, with the same fingerprint, the same header lines and the same
`KEY/FRAG` word as the panel; the P1 store appended all of them. A declined confirm cancelled the request and the dashboard showed
"Cancelled". The workspace line reads "workspace (None) <uuid>" because the stub's registry pin has no name or prefix. The stub signer
(`python -m devhost sign`) stays for the dev loop and the tests.


### M3 fix: idem retry (stream A #402)

`human.prepare` honours `opts.idem` (host contract 0.7/0.8.1): the same principal, idem and arguments return the same request
in its current state; other arguments are `idem.conflict`; an ended request answers in its terminal state for 24 h, then
`idem.expired`; a failed commit or host restart never makes a second request. The server's orphan-cancel recovery (`recover`,
the `Lost` window, the attempts bookkeeping and the panel's "earlier attempt may still be open") is gone. After `outcome.unknown`
the next click retries with the same idem and adopts the request the host returns. The server sends the host
`a_` + a hash of (session token, workspace, browser idem), so another session's identical idem is another request and M1 holds
(a request is reachable only by the session that prepared it). Per-session quota and the logout cancel are unchanged. Panel: a
retry may come back `submitted`, `cancelled` or `expired` and says so; `idem.conflict` is an error, `idem.expired` says "This
attempt is too old" and starts again only on the click with a new idem. The dev stub implements the same (records kept 24 h,
written beside the dev state so a restart never makes a second request). A prepare has no `outcome.get`; the state is `human.pending {request_id}`.

## P2 server (M4 settings)

Reduced workspace settings and the switcher by UUID (#306), on host contract v1.5. Same hand-off machinery as M3; the dashboard
signs nothing and never calls `human.submit`.

| Route | Host op | Notes |
| --- | --- | --- |
| `GET /api/w/<uuid>/settings` | `workspace.settings` (cap `settings/1`) | durations, `defaults`, the four gate policies (canonical), repos as `{name, default_branch, remote}` (follows `next_page`, at most 8 pages, `repos_more`), `settings_at`/`policy_at`. Without the cap: `{available:false}` and every other field `null`. A repo `path` is never forwarded; a `remote` only as a canonical identity without userinfo |
| `GET /api/w/<uuid>/members` | `member.list` | person, name, role, devices `{id, auth, scopes, valid}`; no `last_seen` |
| `GET /api/w/<uuid>/grants?active=` | `grant.list` | what the host gives this person (owner: all; others: their own). `read_ext/2`: `revoked{by,at,seq}`, `session_ids` (cap 100), `sessions_more`; absent = `null` |
| `POST /api/w/<uuid>/handoff` | `human.prepare` | new actions: `settings_set` (grant_hours 1..24, claim_ttl_min 15..1440, lease_ttl_min 5..1440; `repos` refused), `policy_set` (one gate, the F1 5.7 `Policy` in canonical form; code: `not` has `assignees`, `independent: true`), `member_add` (name, role, `pk` = base64url of 65 bytes, `cert` = strict JSON object <= 16 KiB; `pk` is an argument and the certificate goes in `files.cert.b64`, argument `cert: "-"`), `member_role`, `member_remove`, `grant_revoke` (optional reason) |

The host's prepared events are checked against the ask before they are shown (`handoff.expected`): type, person, role, values,
the certificate. Features: `settings`, `members`, `settings_read`, `grant_list`, `read_ext2`, and `do:<op>` for each inner op the host
lists in `welcome.ops` (R5 pending for `policy.set`/`settings.set`). Cancel on switch and the per-session ownership are unchanged.

Browser: `src/app/pages/settings/host/` (General, Members, Gates, Grants), a `?ws=<uuid8>` hint when workspaces share a prefix
(`api/workspaces.ts`, `app/urls.ts`, `app/workspace.tsx`), switcher rows for stopped hosts, `useHandoff` cancels on a workspace
change. Live: `settings.changed`, `policy.changed`, `grant.*` refetch the settings queries; the grants list also refetches on focus
and every 30 s (a non-owner is not sent others' grant items).

Dev stub: `devhost/settingsops.py` serves the reads and the six operations on the P1 model (the certificate is checked with
`check_member_added`), serves several workspaces from one process (`python -m devhost WS1 WS2 --person P1 --person P2`; a file named
`stopped` in `<run>/<uuid>/` takes that workspace down, "host not running"), and the stub signer signs the workspace operations.
Real `orch sign`: signs `member.*` and `grant.revoke`; it refuses `settings.set` and `policy.set` (not in the signer's table until R5).

Open for stream A: `member.add` takes the key as `pk` (argument) and only the certificate as a file today; confirm that is the R9 shape
(the brief spoke of both as files). Confirmation word for `settings.set`/`policy.set`. `human.pending` carries no origin.

### M4 fix round 1 (Opus security review)

L1: when a prefix is shared the switcher trigger, its tooltip and the hand-off dialog name the workspace with its `uuid8`. L2: the ticket
loader reads nothing when the key's prefix is shared and no `?ws=` names one, when the hint fits none, or when the host is stopped (and
`pickWorkspace` likewise on a hint that fits nothing). L3: host strings on the settings pages (remote, branch, grant scope, device id/auth/
scopes, names, session ids) render through `Raw`; the server refuses U+202A-202E, U+2066-2069, U+200B-200F, U+2028/2029 and U+FEFF in a
branch, a remote, a one-line typed text, and checks device/auth/scope/grant-scope words. I-3: a pydantic `ValueError` in the settings
adapters is `internal`, not a 500. I-5: `PolicyBody.canonical` docstring. I-2: `idem.conflict` on a prepare shows "Changed since your first
try" and "Start again" (new key on the click).

**L4 historical limit (superseded by M5):** the intent now comes from canonical host content and gate/question
version only. Neither the dashboard link nor the signer sends `auth.session`; a dashboard restart returns the same
host request for the same intent. The old session-token-derived key no longer applies.

Confirmation words (stream A decision): workspace `settings.set` = `<PREFIX>/SETTINGS`, `policy.set` = `<PREFIX>/POLICY` (prefix from
the signer's own registry); the panel shows them and the stub signer asks for them. Real `orch sign` learns them with H6b. `member.add`:
`pk` is a plain argument and the certificate travels only in `files.cert` (the argument carries `-`); a test checks it is never an inline
argument, and the stub refuses an inline certificate with `blob.invalid`.


## P2 on the real host (stream B, milestone RH, 12 Oct)

The dashboard now runs against core's real host (H6b #404, H4b #409) with the real `orch init`, CLI and signer. Everything here was done
on disposable workspaces (explicit `ORCH_STATE_DIR`, `HOME` and `XDG_CONFIG_HOME`, `.orch-dev` markers, never `~/.config/orch`).

**How it is driven.** `server/tests/realhost_kit.py` creates a workspace with `orch init` in a pty (it types the passphrase, the three
recovery words and the "Type KEY and Enter" confirmations like a person), grants the dashboard addon, seeds tickets in every status with the
real CLI (an agent grant, questions, gates, tasks with receipts, evidence, a closed ticket), registers the workspace and starts
`orch host run --foreground` as a subprocess it stops by its own PID. `python tests/realhost_kit.py up|open|down` does the same for a person.
The base directory must be short and outside /tmp (sockets are about 100 bytes; the host refuses a run directory under a sticky ancestor), so
it lives in `~/.cache/orch-rh/` (or `$XDG_RUNTIME_DIR/orch-rh/`). `server/tests/test_realhost.py` (marker `realhost`, skipped when the host
cannot start) is the integration suite: `cd server && uv run pytest -m realhost -q` (about 1 minute). It is NOT in CI (`dashboard.yml` runs
`-m "not realhost"`): it needs a pty, unix sockets and a state directory in the home folder. Offline, `test_realhost_goldens.py` replays
scrubbed real answers (`src/test/fixtures/real-host/raw.json`, re-record with `UPDATE_REALHOST_GOLDEN=1 uv run pytest -m realhost -k record`).

**Verified on the real host (checks in the suite).** Launch and session; workspaces, `me`, identity; Today (question, approval, `no_eligible`
as a notice); board and backlog with paging (`next_page`), closed never on the board; ticket page (sections, tasks with receipts, acceptance
proven, artifacts with digest and bytes, the log; a closed ticket reads like any other); an evidence file read and digest-checked; settings,
members and grants reads (`read_ext/2` revoked fields); a CLI write through the running host reaches an open stream within 2 s; a cut stream
resyncs; every hand-off signed with the REAL `orch sign CODE` in a pty: approve, request changes, answer, comment, label, close, reopen, new ticket,
Complete details (`set`) and a section (`section.set`), verdict pass and fail, member add (certificate file), grant revoke; `settings.set`,
`policy.set` and `move` signed with the real signer (`<PREFIX>~<WS8>/SETTINGS|POLICY`, H6b knows them); two workspaces with the same prefix and the
same ticket key; a workspace whose addon is disabled is listed unavailable while the other works; with the host stopped nothing is available.

**What changed in our code because of the real host.** (1) The host serves the `human.*` ops and lists them in `welcome.ops`, but `welcome.caps`
has no `human/1`: `HostLink.supports` accepts a listed human op (RH-1). (2) The host lists `diff/1` (for `artifact.get`) but serves no `diff`
op: the `changes` feature is off and the ticket page has no Changes tab (it used to draw one by status and then fail with "Could not load the
diff") (RH-2). (3) `move` is offered when `welcome.ops` lists it (features `move`, `do:move`): a typed `move` action (`backlog` or `open`,
optional reason), the prepared event must be `ticket.moved` to that target; on the board a drag or the card menu opens the hand-off dialog
for Backlog or Open (other columns say so), and nothing moves in the page until it is signed. The palette move and the bulk move are off in
hand-off mode. (4) `needs` on a real host: a `no_eligible` notice counts in the workspace's `needs_you` number but not in Today's "N need you".
Not changed, noted below (RH-6).

**Gaps for stream A (nothing in core was touched).**

| # | Gap | Effect here |
|---|---|---|
| RH-1 | `welcome.caps` lacks `human/1` although `human.prepare/get/submit/cancel/pending` are in `welcome.ops` (contract section 0.3 lists the cap) | Worked around (a listed human op counts); remove the exception when the cap is listed — **fixed in core #416** (welcome lists `human/1`); the exception stays harmless (bounded to `human.*` ops) and can go |
| RH-2 | `diff/1` is listed but `diff` is not served (known: "H4a2 lists diff/1 for artifact.get before diff is served") | **Fixed in core #417** (the host serves `diff`; the Changes tab and `/changes` work). Was: no Changes tab, 501 on `/changes` |
| RH-3 | `human.prepare` with an `idem` for `move`, `policy.set` and `settings.set` raises `KeyError: unknown operation` in the host (`human.py _begin_idem` looks the op up in the ops registry, which does not hold the three host person ops): the caller sees 500 `internal` "Request handler failed". Without an idem the same prepare works and the real signer signs it | The dashboard always sends an idem, so Settings (grant length, policy) and Move cannot be prepared from the browser on this host. Strict xfails in `test_realhost.py` (`HOST_PERSON_OPS_IDEM`) flip when it is fixed. Direct prepares without an idem prove the rest — **fixed in core #416**: the realhost tests prepare settings, policy and move through the dashboard (xfails removed) |
| RH-4 | No op changes a ticket's visibility (`visibility.changed` is a person event with no CLI or host op) | A hiding that purges a tab cannot be produced from a single person on a real host; covered with fakes in `test_live_updates.py` |
| RH-5 | A host cannot stop one workspace: there is no per-workspace "stopped" state; a disabled addon is the nearest (`addon.disabled`), a stopped host takes everything | S13's "one host stopped" is exercised as addon disabled plus the whole host stopped |
| RH-6 | `member.list` carries the same names the dashboard shows; the workspace `needs_you` count includes `no_eligible` notices | The switcher badge can differ from Today's count; counting only actionable kinds needs the kind list from the host | **Fixed (M5):** the server sums only `question` and `approve` items (viewer: 0), exactly Today's count; `no_eligible`, `acknowledge`, `addon` notices are excluded.
| RH-7 | `orch task done` in host mode answers not_implemented (H3b2 #412) | Seeding done tasks happens before the host starts |
| RH-8 | `workspace.show` has no `created_at`; the workspace name is the directory name | Shown empty |

**Dev stub vs the real host** (the stub still serves CI and the mock app): it lists `human/1` and `diff`, serves `diff` against the dev
workspace's git state and so shows a Changes tab; it has no `move`, no `workspace.settings` write path for `settings.set` and `policy.set` that
the real host cannot prepare with an idem (the stub accepts the idem), keeps requests in memory, signs with its own `devhost sign`, uses a
registry that pins no name or prefix, and its `needs`/`ticket.query` snapshots are simulated. Treat the stub as a development adapter only:
acceptance is the real-host suite and the Playwright scenarios in orch-dev-kit (`e2e/dashboard/`).

## Click-to-sign (H5c, M5, 12 Oct)

Owner decision of 12 Oct 2026 (coordination log "OWNER RE-PLAN P2", #264 comment 6121460551): in phase 1 the dashboard signs with one click.
Source of truth is `docs/architecture/orch-v2-host.md` section 0.8.6; this is how the dashboard meets it.

**Trust model.** The dashboard (this server and the browser) is trusted with signing authority; core's separate signer child holds the key
(never this HTTP process, never the browser). Card text, also the first line of every sign panel, verbatim: "Convenience signing: agents
running as you, or anything that takes over this dashboard, could sign."

**Pieces.**
- `server/src/orch_dashboard/click_signer.py`: one signer child per server (`orch.click.spawn.start()`), started on first use, restarted
  if it exits (open approvals become terminal-only), stopped with the server (`ClickClient.close()`, which kills only that child's PID).
  Refuses a signer whose `status.contract != 1` ("update orch-core"). The child gets our environment without `PYTHONPATH` and with
  `ORCH_STATE_DIR` set to the registry this server reads. `ORCH_DASHBOARD_CLICK=off` never starts one (the tests use it).
  Never run the dashboard with a `PYTHONPATH` pointing at another core: the child runs `python -I -m orch.click` from the dashboard
  venv's own orch-core, and orch-core is a runtime dependency (`orch-core>=2.0.0.dev0`, editable path source).
- `server/src/orch_dashboard/click.py`: the closed routes `POST /api/w/{ws}/click/{prepare,view,sign,cancel}` and `GET .../click/status`.
  `prepare` takes the same typed actions as the hand-off (`PrepareBody`); the op is chosen server-side (`host_call`), the idem is the
  action intent's (see "The invariant" below), files for `member_add`. `view`, `sign` and `cancel` work on requests the PERSON prepared in this
  workspace, from any of their sessions (otherwise `not_found`). `view` passes the signer's View through unchanged (the server only checks that it names the
  op it prepared). `sign` answers `{state, duplicate, key?, seq?}`, never the host's result. The open-request quota is one per session,
  shared with the hand-off (reserve, commit, release under one lock). Views, words and credentials are never logged.
  Only these handlers call the signer (a test greps for it); no addon path reaches `prepare`.
- The per-workspace flag: `GET /api/workspaces` carries `click` (custody, enabled, available, problem) from the signer's `status`, and the
  feature `click` when the flag is on and the signer can sign there. The UI offers Sign only then (`clickReady`).
- UI: `HandoffDialog` (all signed actions) offers **Sign** only where click is on (no terminal button; flag on but the signer unable: an honest message, nothing prepared). An action with nothing to fill in no longer starts the terminal
  request by itself when Sign is offered. `ClickSignPanel` shows ONLY the View: the risk line first, op, WS8 badge, prefix and name through
  `visible()`, fingerprint, `lines` as text, checks with their time, artifacts, expiry; word ops show the View's word and a field (sent
  exactly as typed). Sign is armed after 500 ms of visible presentation (the timer restarts on blur and when the page is hidden); a
  press that began before it was armed, an auto-repeat and a bare script click are ignored; a new View resets everything. No URL or query
  opens a panel. `api/click.ts` maps every code and reason of 0.8.6 to one honest message and one next step; a refusal never cancels, only
  Cancel does (and closing after `uncertain` does not cancel either). The workspace card (Settings, General) shows the risk line when
  the custody is `convenience`.
- HTTP (item 7): CSP with `object-src 'none'`, `form-action 'self'`, `frame-ancestors 'none'`, `base-uri 'none'`, `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy: no-referrer`, `no-store` on `/api` and `/auth`; exact Host and Origin, `null`/missing Origin refused on
  mutations; `Sec-Fetch-Site` enforced when present on API calls; every POST is `application/json` and bounded (16 KiB on view, sign and
  cancel); no CORS; the bearer only in tab memory and `sessionStorage`.

**Tests.** `tests/test_dashboard_click.py` (faked channel: lifecycle, routes, error mapping, parity with the hand-off gates, the quota under
concurrency), `tests/test_dashboard_http.py`, and `tests/test_realhost_click.py`: a REAL host and the REAL signer child. On macOS core's
convenience store is the login keychain, so `tests/click_child.py` swaps core's documented test seam (`orch.custody.convenience.open_store`)
for the 0600 file store of the disposable state; the keychain is never opened. The key is moved with the real
`orch keys custody convenience` in a pty. Frontend: `components/sign/click-*.test.tsx`, `production/click.test.tsx`.

**The invariant: one action intent -> at most one request -> at most one signature.** It is held by the HOST, not by this process:
the host's `human.prepare` idem record (keyed by principal and idem, bound to op, args, signer, handoff, person, files and session) returns
the same request, or its terminal state for 24 h, to every caller of the same idem; the signer child forwards the idem and signs a request
once (its own `approval.result`/`signed` record, and the host refuses a second submit). The idem is derived in `server/src/orch_dashboard/intent.py`
from the action (`person`, `workspace_id`, `op`, `args` exactly as sent, `base` = gate gen/hash for decisions, question id/hash for answers, null otherwise) and is the
same for every tab, session and retry; the browser sends no key, only `retry`, which an explicit "Do it again" / "Start again" advances
(`handoff.py` `plan()` and `send_prepare()`, used by both `click.py` and the hand-off route). Consequences, all tested: two tabs prepare one
request; a lost answer or logout/login returns the same request; a landed action answers `intent.done` ("Already done"); cancelled/expired
is stepped past by `send_prepare()` (#794): pressing again is one fresh request, the ended one is never signed; `idem.conflict` (other path or signer binding) becomes `intent.open` only while a matching request is open,
otherwise `intent.blocked` offers explicit Start again; neither case automatically prepares;
`idem.expired` becomes `intent.expired` (HTTP 409), with no new prepare; only an explicit "Start again" advances `retry`.
Requests belong to the (person, workspace) (`Handoffs.owned`); any session of the person may view,
sign, poll and cancel. **What stays in memory is convenience only:** which ids this dashboard prepared (ownership, lost on restart), the
op/body a View is checked against, the per-session quota, and an `unsettled` set (a sign in flight or with an unknown outcome) that only
stops this process from cancelling the request or replacing the signer under it and drives the "settling" UI (`POST /click/state` reads
the host through our link). A sign whose answer is lost is retried through the signer (identical events, never a new signature);
`already_signed` (signed with another View) and `record_unavailable` (the signer's record is unreadable, fail closed: use the terminal path)
are mapped. A dashboard restart loses the in-memory parts and the session binding of the host's idem: a repeat prepare is then `intent.open`
until the old request settles (at most about 10 minutes); the ticket's activity shows a submit that landed. **One signing path per workspace** (owner decision 12 Oct, withdrawn 24 Oct by the owner, #1170): with the signer's flag ON the dashboard signs by click, and the Terminal hand-off (`orch sign CODE`) stays available for the ops One click never signs (terminal-only) and as a backup; `Gateway.one_click_on` only picks the default sign method. The intent function is identical on both (no path offset). A click prepare first lists `human.pending` and refuses if a
terminal hand-off (it has a code) of the same op and key is still open (`intent.open`, with its expiry); a flag flipped off ends open click
requests (the signer refuses with `disabled`; the server cancels them).

**Open points.** The rate limit (30 per minute) is mapped and unit tested, not provoked on the real signer. The server cannot show the
prepared events of a click request (the signer holds them and validates them), so the hand-off's `expected()` event check is replaced by the
signer's own checks and the op comparison on `view`.


M5 final intent contract (owner decision 13 Oct ~17:20, refined by orch-d6; coordination.md 'M5 'same intent'' and 'M5 intent REFINED'):
**one intent (op + content + gate/question version) → at most one request → at most one signature;
a deliberate repeat only via 'Do it again'.** Content means the exact canonical host arguments and certificate bytes.
Gate decisions use gate generation/hash; answers use question id/hash, never its answered flag or the whole ticket.
All other ticket operations (log, set, section.set, close, reopen, move) and workspace operations
(settings, policy, member, grant) have no base. Unrelated agent edits cannot produce a second request.
The host retains the intent outcome for 24 h. Both panels show server-sourced signed content and signing time
before 'Do it again' advances retry. Neither dashboard link nor signer sends `auth.session`: restarting the
dashboard reconnects with the same binding and returns the same request, not `idem.conflict`.
A conflict is checked against `human.pending`: prepared/bound requests of the same op+key show their expiry;
otherwise `intent.blocked` offers explicit 'Start again', warning that an earlier request may have been signed.
No automatic retry advances the intent. Hand-off expiry is decided by host state, never the browser clock.

**Host API gap:** current `human.get`/`human.pending` expose creation/expiry but not signing time.
The server returns `signed_at: null` and the panels say signing time is unavailable; they never call creation
time signing time. A host `signed_at` field is required for accurate 'N min ago'. The submitted click prepare
loses its request id in the signer error, so the server replays the identical host prepare binding/idem through
its own link to retrieve the exact existing record, not an arbitrary op+key match. Its warning summary comes
from that record's material review. `human.request_state` at sign reads pending/get before offering a repeat.

**F4 merge prerequisite: host PR #434 must merge before M5.** The existing local fail-open behavior is unchanged:
(the one-signing-path rule this section guarded was withdrawn 24 Oct, #1170: a hand-off on a click-enabled workspace is allowed). The dashboard's shared idem and op+key
pre-check remain defence in depth; they are not a replacement for this host prerequisite.

**Known limits after the final Opus re-check (13 Oct, all LOW, none can produce a second signature):**
- N1: "Already done" is also shown when the state was reverted in between (role B→A→B, close→reopen→close). The
  warning describes the earlier signature; "Do it again" is offered, so nothing is blocked. The
  host `submitted_at` (#437) now supplies relative signing time with an absolute-time title, only when
  `human.pending.seq != null` confirms a committed outcome (§0.8.1). Missing times remain unavailable.
  Submitted failures show `intent.failed`, the host catalog code, and Start again (advances retry), with a
  warning that nothing was signed into the log. `lock.busy` is retryable and hand-off polling continues.
  Host convenience-reader fix text is displayed verbatim through Raw.
- N5: a label add sends the full label set, so a concurrent label change in another tab is different content and
  therefore a different intent (by the owner's definition). The host's `base_rev` on `ticket.updated` refuses the
  stale one at commit.
- F4: the server treats an unknown signer status as "click off" (fail-open for the path choice). Core #434 (merged)
  enforces one path per workspace at the host, so a stray hand-off request can't be prepared there.

### T3 navigation and signing cleanup — 13 Oct 2026 (#444, #447 I-6)

Every shell route serves HTML with unchanged CSP and `no-store`. Present Fetch Metadata
other than a single exact `none`/`same-origin` value (including empty, same-site, cross-site,
unknown, duplicate or comma-joined values) adds `<meta name="orch-nav" content="foreign">`
in the head per request, regardless of mode/destination. Assets are unchanged. Absent
metadata stays untagged for compatibility; older browsers or header-stripping clients
therefore lack this protection.

The server marker is authoritative for the document's bootstrap: foreign tabs clear any
inherited session storage and never open BroadcastChannel, request tokens or receive offers.
There is no requester assertion that can override it. Untagged tabs share sessions as before.
Foreign tabs may exchange their own launch fragment via the same-origin auth API, supporting
the private file launcher's cross-site navigation. Without a session they show terminal
(`orch-dashboard open`) / bookmark guidance. Tokens are never embedded in HTML or URLs.

Definite pre-dispatch/signer failures clear newly created unsettled marks before follow-up
reads, but retain prior unknown-outcome marks on retry. Channel loss/timeouts/internal errors
remain uncertain until settlement/expiry. A non-dict initial signer status closes the newly
spawned child before reporting unavailable.

## P2 addons: quick tasks and records (M6, 13 Oct)

Stream B M6 on the real host (host contract 12.8, 12.9, 12.11 "Rules and strings"). Server by Codex (routes and the records child,
commits 12208b39, 3e76f013), UI and tests here. The mockup's addon pages for quick tasks and records stay as they are in the mock; on a
real host they are native pages in the same style (the mock's pages are declarative node trees with no host route behind them).

| Page | Gate | Reads / runs |
|---|---|---|
| `/quick` (sidebar "Quick tasks", real host only) | feature `quick` (host caps `quick/1` and `quick.list`) | `GET /api/w/{ws}/quick?status=..&max_commits=..&max_files=..&limit=..&page=..` (flat limits, per the H11f as-built contract) |
| Settings, Records tab | feature `records` (`records/1` and `records.status`); owners and maintainers only | `GET /records`; `GET /records/current` (authorized, bounded status child); `POST /records/commit {push}`; `POST /records/auto-off` with no body |
| Settings, General, "Dashboard execution" | `runner` from `/api/workspaces`, `/api/me`, identity (`whoami.runner`, cap `run/1`) | draws the D-P2-31 line verbatim whenever `runner.enabled`; nothing when the host says nothing |

- **Per-workspace switch:** nothing here is a dashboard setting. The pages exist only where the host advertises the caps and ops, which the
  host does per workspace and off by default. The dashboard has no way to turn them on; `orch runner enable`, `orch records auto on`
  (`--push`), `orch records remote private`, `orch records push --initial` are shown as exact commands, never as buttons.
- **Quick limits** are a local browser setting (`orch.quick.limits.<ws>`, try/catch around storage; unset means the host's defaults).
  Counts the host cannot read are "unknown", never zero; `over` null is not "outgrew".
- **Add a quick task** (a person): `new` (chore) then a second signed change setting the label `quick` (`details` with `labels:["quick"]`,
  follow-up through `CompleteDetails`). Both use `HandoffDialog`, so click-to-sign where it is on and the terminal where it is off; the UI says
  "two signatures". Close uses the existing `close` hand-off. "Make a normal ticket" (orch-52): there is no
  remove-label op, so it is `details` with `labels` set to the remaining labels (replace-only). With `quick` alone, `labels:[]` reaches core as `labels=` (#454).
- **Records strings, verbatim:** the export warning (shown on the page whenever a push is configured, and again in the confirm dialog before
  "Commit and push") and the private-remote line, each with the exact URL drawn through `Raw`. A URL the server withheld (hidden characters) is
  said to be withheld; no URL is invented. Child failures show the stable code, the child's message only when it was safe (`message_hidden`),
  and the local commit that landed when only the push failed (`error.commit`).
- **`records.status.configured`** means "the workspace records repository exists" (core #454: `ok`, `index_differs` and `unsafe`, even before a commit).
  false reads "No records repository yet; the first commit creates it." with Commit now; `last == null` reads "nothing noted yet".
- **What was dropped from the mock honestly:** own Q-001 keys and statuses, the "agents may add" setting, records settings and pull.

Tests: `src/production/quick-records.test.tsx` (31 cases: states, strings, limits, two-signature add on click and terminal workspaces, role,
absent caps, runner line), `server/tests/test_realhost_quick_records.py` (real host: seeded `quick.list`, `records.status`, a commit into the
workspace's own repository, a push to a LOCAL bare repo refused and nothing published), orch-dev-kit `e2e/dashboard/dashboard_quick_records.spec.ts`.

**Layout coverage of the real-host-only UI (M6):** route `/__layout/<scenario>` (`src/dev/layoutPreview.tsx`, component and fixtures included only when
`import.meta.env.DEV`; in production the route remains registered but only redirects to `/`) draws the real quick page, Records tab and runner card with fixture answers (it swaps the
mock api's methods per scenario). `scripts/layout-guard.mjs` checks the `real-host-*` pages: quick, many rows, limits open, normal-ticket
and add (first signature) dialogs, records (long URLs), hidden URL, first commit, push confirm, failed push, runner on/off.

M6 follow-up (#454, 2026-10-14): `GET /api/w/<ws>/records/current` checks `records/1` and `records.status`, authorizes with the host, then runs `python -I -m orch.records status --workspace <id> --json` under the same isolation, 60-second budget, output cap and workspace guard as commit. Its strict success envelope is `{ok:true,detail:{status:…}}`; only validated status fields reach the browser (`local` is discarded). The confirm dialog checks on each opening and uses `remote.current_url`, `current_private`, and `matches_last`; unavailable/hidden current URLs retain the historical warning with a failure code. The page outside keeps the last-known labels. No new events.

## Terminals on the real host (M7a, 14 Oct)

Stream B M7a (host contract v1.6 §12.3, §12.6; H11b). The dock is real when the server lists `terminals` (host caps `run/1`, `term/1`
and the seven ops below); otherwise it is not drawn on a real host. Start agent is M7b (next section).

| Route | Host op | Notes |
|---|---|---|
| `GET /api/w/{ws}/runs` | `run.list {state:"all"}` (paged, 8 pages max) | runs projected (`adapters/runs.py`: no grant, session, person); `runner` = `{connected, enabled, terminals}` |
| SSE `/api/events` event `runs` | `run.sub` (opened before the page lists, D-P2-37) | items `{t:"run", run}` / `{t:"gone", run}`; the page merges by `rev`; `resync`/`ready` read the full list again |
| `POST /api/w/{ws}/terminals/preview` | `term.preview` | `display`, `cwd_path`, `notice`, `blocked`, `warnings`: untrusted text, hidden characters withheld |
| `POST /api/w/{ws}/terminals/open` `{…, spec, idem}` | `term.open` (idem create) | the page draws one `a_<ULID>` idem per confirmed preview and reuses it on a retry |
| `POST /api/w/{ws}/runs/{run}/stop` | `run.stop` | `{state, cleanup}`; incomplete/unknown → "Some processes may still be running" |
| `POST /api/w/{ws}/runs/{run}/attach` `{mode, take?, from?}` | none | the browser ticket of §12.6.6 step 1 (128 bits, sha256 kept, single use, 30 s, 10/min/session, bound to ws/run/mode/take/from/session/server generation) |
| WebSocket `GET /term` | `term.attach` (own host connection), `attach.cancel` on every teardown | §12.6.6 checks in `Security.websocket`; uvicorn `ws_max_size=65536`, `ws_per_message_deflate=False`; dependency `websockets==13.1` |

- **Bridge** (`server/src/orch_dashboard/terminals.py`): `TermSocket` per WebSocket; a `Pump` thread owns core's `TermClient` after
  `connect` (input, resizes, throttled resends, continuous reads); `OutQueue` keeps 512 KiB of decoded output (oldest → one `gap`),
  control frames never dropped. Close codes: `CLOSE_TABLE`, verbatim from §12.6.6.
- **Page** (`src/app/terminal/real/`): `termProtocol.ts` (messages, close policy, reply filter), `termView.ts` (xterm per §12.6.7),
  `RealTerminal.tsx` (lease UX), `RealDock.tsx` (runs, preview, stop). `src/api/runs.ts` holds the routes and the live merge.
- **Tests:** `server/tests/test_terminals.py` (table, tickets, socket checks, queue, sink), `test_terminals_host.py` (core host + runner in
  process: lease, 4409, resize, 4401, replay of an exited run, stop, live `runs`, the log canary), `test_realhost_terminals.py`
  (`-m realhost`); page: `src/app/terminal/real/*.test.ts(x)` including `canary.test.ts` (§12.6.7 corpus with a real xterm in jsdom),
  `src/api/runs.test.ts`, `src/dev/layoutPreview.test.tsx`. Layout guard: `real-host-terminal-*`.
- **Requests for stream A:** (1) a dev-only seam so `orch host run --foreground` can admit a foreground runner (`orch runner run
  --foreground`) without a service manager, for a subprocess real-host test; (2) `term.attach`'s refusals could carry `retry_after_ms`
  for `rate.limited`/`attach.limit` (the server passes it on when present, else 1000 ms).

The dashboard and devhost use core's `ORCH_HOST_RUN_DIR`, falling back to `<state>/run`.
A set legacy `ORCH_RUN_DIR` is accepted silently only when its absolute path matches that directory;
otherwise dashboard startup exits 1 with a migration message.

## Start agent on the real host (M7b, 15 Oct)

Host contract v1.6 §12.3/§12.4 (H11c #466), D-P2-27 (the runner grant), D-P2-31, §0.8.6 item 6 (the click signer's deposit-only grant
mode), changelog 40; H11c-2 (core #488, not merged) for signer contract 2, `deposit_reason`, `grant.deposit_status.reason`,
`grant.list.label` and the `run.sub` `{t:"runner"}` item. Features: `agent_start` (`agent.preview`, `agent.start`, `run.list`,
`whoami`) and `runner_grant` (`grant.deposit_status`, `grant.forget`, `whoami`). The host link sends these four ops under `run/1`;
**`grant.deposit` is never sent by this process** (`hostlink.FORBIDDEN`, refused in `HostLink.call` before any I/O).

| Route | Host op / signer frame | Notes |
|---|---|---|
| `POST /api/w/{ws}/agents/preview` | `agent.preview` | own body model: harness `claude-code`/`codex`, mode `refine|work|fix|continue|context`, model, subagent_model, label ≤40, worktree (default true). Projection `adapters/runs.agent_preview`: `spec, ticket, display, cwd{kind,repo,ticket}, cwd_path, will_create_worktree, login_shell, env` (names), `grant{id,until,scope}`, `blocked{code,message,detail{reason?,missing?}}`, `warnings` ≤8; never `argv`/`program`; hidden characters withheld |
| `POST /api/w/{ws}/agents/start` `{…preview args, spec, idem, cols?, rows?}` | `agent.start` (idem write) | one `a_<ULID>` idem per dialog opening, reused on every retry (run id `r_` + its ULID); `launch.changed` → the page previews again |
| `GET /api/w/{ws}/runner` | `whoami` | `{runner: {connected, enabled, terminals, grant}|null}`; re-read on every live `resync`/`ready` (a runner restart drops the grant) |
| `GET /api/w/{ws}/runner/deposits/{deposit_id}` | `grant.deposit_status` | `{deposit, reason?}`; `reason` only when rejected, from the closed set, else `other` |
| `POST /api/w/{ws}/runner/forget` `{grant, idem}` | `grant.forget` (idem write) | "Forget runner grant", after a confirm |
| `POST /api/w/{ws}/click/prepare` `{action:"runner_grant", hours 1..12 = 8, scope workable|all}` | signer `prepare {op:"grant", args:{hours, scope}, deposit:true}` | click only (not a hand-off action); a signer below contract 2 → `human.click_refused` `terminal_only` with `why:"update_orch"` |
| `POST /api/w/{ws}/click/view` | signer `view` | `expected()` checks the host's prepared `grant.issued` (label runner, verbs agent, the hours and scope asked, 1..12 h, actor = the caller); the View must have `deposit:true`, `deposit_line` = core's constant (also in `lines`), the owner's risk sentence in `lines`, and the word `<PREFIX>~<WS8>/GRANT`; a non-grant View claiming a deposit is refused |
| `POST /api/w/{ws}/click/sign` | signer `sign` | a grant's answer is only `{state, duplicate, deposit, deposit_id (= request ULID), grant (gr_ id), deposit_reason? (rejected only, closed set)}`; never `result` or anything else |
| SSE `runs` | `run.sub` | `{t:"runner", grant}` items (H11c-2) patch the runner card; run rows gain `grant_state` (`ok|ended`); `session` and the run's grant id stay out |

- **Page:** `src/app/terminal/real/StartAgentDialog.tsx` (core's dialog: preview, approved texts, blocked, Start, the runner grant card),
  `src/api/agents.ts` (routes, texts). Opened from the dock's **Start agent** and the ticket's **Actions → Start agent** on a real host.
- **Signer contract:** `click_signer.Signer.contract` and `deposits()`; the check that accepts contract 2 is the coordinator's own PR.
- **Tests:** `server/tests/test_agents.py`, the M7b block of `test_dashboard_click.py`, `test_realhost_agents.py` (core host + runner +
  core's ClickSigner in process, fake harness); page `StartAgentDialog.test.tsx`, `src/test/signing-surface.test.tsx` (start agent),
  `src/dev/layoutPreview.test.tsx`. Layout guard: `real-host-agent-dialog|blocked|grant|outcomes`.
- **Requests for core:** (1) the `deposit_reason` sentence set is the dashboard's own wording (core sends codes only); (2) `run.limit`
  detail does not reach the socket (#479): the host's message is shown as text.

## Pair a phone (pair/1, contract v1.9, fixtures only, 15 Oct)

Settings → Devices (`src/app/pages/settings/host/Devices.tsx`). Gate: feature `pair` **and** `pairingApi.served()`; the real api
(`src/api/pairing.ts`) answers `pair.unavailable` until a host serves them, and the server lists no `pair` feature and has no route.
Host ops (names confirmed by orch-52: cap `pair/1`, class P, human_only; none served, none called):

| Page call (`pairingApi`) | Proposed host op | Notes |
|---|---|---|
| `preview(ws)` | `pair.preview {}` | `{blocked, warnings, scopes_default, ttl_max_ms, live_offer}`; convenience blocks only for flag/custody (assumed `blocked.cause`); history is a warning |
| `offer(ws)` | `pair.offer {scopes?, ttl_ms?}` | `{offer_id, expires_ms, native_link, link}`: the link; never cached, stored or logged (server: no body logging, `no-store`) |
| `list(ws)` | `pair.list {}` | rows `{offer_id, state, created_ms, expires_ms, scopes, label?, sas?, cert_code?, reason?}`; assumed `cert_ready`, `needs_remove` |
| `answer(ws, id, approve)` | `pair.answer` | only after the typed SAS matches; Reject default; 3 typed mismatches refuse (assumed `mismatches` arg) |
| `cancel(ws, id)` | `pair.cancel` | |
| `members(ws)` | `member.list {}` (read_ext/3, v1.9) | `devices[]` carry `label, paired_at, relay, cert_expires_at, reader` (H12d-2 shape); listed when `relay` is true |
| `subscribe(ws, fn)` | `sub.open {topics:["pair"]}` | items `{t:"pair", offer_id, state}`, no secrets; the page refetches `list` |
| hand-off `pair_device {offer}` | `human.prepare {op:"relay.pair_device"}` | F1 `device.added`; terminal only (not click-eligible) |
| (none) | device removal | CLI only this round: the page shows `orch device remove <device-id> --workspace <PREFIX>` (assumed) |

Fixture: `src/dev/pairFixture.ts` (`createPairFixture`, `installPairFixture`, `emit` drives states). Layout scenarios `pair-unavailable`,
`pair-blocked-convenience`, `pair-offer`, `pair-pending`, `pair-cert`, `pair-failed`, `pair-devices`; guard entries `real-host-pair-*`
(plus `real-host-pair-link` and `real-host-pair-remove-command`; listed in `scripts/layout-guard-pages.mjs` so `--only pair` finds them).
Tests: `src/app/pages/settings/pairing.test.tsx`, the Devices case in `src/production/settings.test.tsx`, `src/dev/layoutGuardPages.test.ts`. QR: `uqr` 0.1.3 (pinned).

## Reader levels and click-to-sign refusals (core H12d-2, 15 Oct)

| Route | Host op | Notes |
|---|---|---|
| `GET /api/w/<uuid>/readers` | `reader.list {}` (cap `reader/1`, read) | `{available, readers:[{device, person, reader, level}], required_reader}`; `dk_hash`, `digest`, `decl` not forwarded; `available:false` without the cap; feature `readers` |
| `GET /api/w/<uuid>/members` | `member.list {}` | devices also carry `label` (withheld → `label_hidden`), `paired_at`, `relay`, `cert_expires_at`, `reader` (null: no verified record) |
| any error | — | `error.detail` only for `sign.convenience_unavailable` / `sign.convenience_readers`: `reason` from the closed set, `devices` device ids (`errors.py` `closed_detail`) |

Not called: `reader.declare`. The click signer's channel carries `reason` at the top level and no `devices` (core's `ClickError`);
the page reads `detail.reason`, else `reason`. `reader.decl_invalid` has its sentence (pairing, member add). The signer's status
`problem: readers_old` is its own sentence with a link to Settings → Devices. Page: `src/api/refusals.ts`,
`src/components/sign/RefusalDevices.tsx`, `src/app/pages/settings/host/Readers.tsx`. Devhost: `reader.list`, `required_reader`
in `workspace.show`/`member.list`, the read_ext/3 fields (`ORCH_DEV_READERS=0` turns them off). Layout scenarios
`readers-devices`, `convenience-refusals`; guard pages `real-host-readers-devices`, `real-host-convenience-refusals`. Tests:
`server/tests/test_dashboard_readers.py`, `src/app/pages/settings/readers.test.tsx`, `src/production/readers.test.tsx`.

## Drop (drop/1, proposal pending, fixtures only, 15 Oct)

Workspace → Drop (`src/app/pages/drop/index.tsx`, route `/drop`, sidebar entry after Quick tasks when the server lists `drop`). Gate:
feature `drop` **and** `dropApi.served()`; the real api (`src/api/drop.ts`) answers `drop.unavailable` for every call; the server lists no
`drop` feature and has no route. Binding decisions of 15 Oct (files only in P4, host cleans names, no auto-claim, Delete through
`drop.retire` pending D39a1, states `live`/`pruned`/`expired`/`retired`) and the assumptions: DECISIONS-LOG "Drop: Workspace → Drop".

| Page call (`dropApi`) | Proposed host op | Notes |
|---|---|---|
| `info(ws)` | `drop.info {}` (assumed) | `{max_bytes, version_cap, writes}`: the only source of the 25 MiB and 1024 limits |
| `list(ws, {direction, after, limit})` | `drop.list` | Received / Sent, cursor pages of 32 |
| `versions(ws, {object_id, after})` | `drop.versions` | documents only; `cap` |
| `blobPut`, `blobDrop` | `blob.put`, `blob.drop` (§0.10, served today) | 768 KiB chunks; `uploadBlob` gives progress and cancel |
| `send(ws, {blob, name, mime, object_id?, base_version?}, idem)` | `drop.send` | the host seals and PUTs ("Sealing and sending…") |
| `read(ws, {object_id, version})` + `content(ws, blob)` | `drop.read` + a future dashboard route | download only (attachment headers in DECISIONS-LOG) |
| `retire(ws, object_id)` | `drop.retire` | files and documents, confirm first |
| `subscribe(ws, fn)` | `sub.open {topics:["drop"]}` | items `{t:"drop", object_id, version}`; the page refetches |

Fixture: `src/dev/dropFixture.ts` (`createDropFixture`, `installDropFixture`, `emit`; options `documentWrites`, `chunkDelayMs`,
`stallAfterChunks`, `sendDelayMs`, `sendError`, `readError`, `retireError`, `listError`, `pageSize`). Layout scenarios `drop-unavailable`,
`drop-list`, `drop-long-names`, `drop-upload-progress`, `drop-errors`, `drop-versions`; guard entries `real-host-drop-*` plus
`real-host-drop-delete` (listed in `scripts/layout-guard-pages.mjs`, so `--only drop` finds them). Tests: `src/app/pages/drop/drop.test.tsx`, `src/production/drop.test.tsx` (real host: not available, no nav entry, no call),
`src/dev/layoutGuardPages.test.ts`. No new dependency.
To do when the ops are agreed: the server's `drop` feature and op passthrough, the download route with the headers above, `realDropApi`.


B10 Part B phase 1: POST `/api/w/:ws/phone/request` forwards `phone.request {ref,gate,gate_gen}` with the browser attempt's idem; POST `phone/cancel` forwards `phone.cancel {id}`; GET `phone/status/:id` forwards `phone.status {id}`. GET `phone/available` counts the pinned person's valid decide-only (plus look) devices. These are person-only, unsigned hints, never events. Requirements/plan only; no operation choice or change text is sent. The host's outcome is authoritative, including after cancel/expiry. Other methods remain available.
## B9 setup HTTP boundary (16 Oct 2026)

The browser opens plain `/setup` and bypasses dashboard shared sessions. `POST /auth/setup {}` reads and deletes the core's private 0600 `<run>/setup.code`, then calls socket-only `POST /claim {code,port}` using the listening port. Only a successful claim registers the returned token as a setup-only hashed credential. The browser keeps that token in memory and sends X-Setup-Token. No launch code is read from a URL or browser body. Core enforces 30-second single-use launch codes and 10-minute idle expiry except while running.

| Browser | Unix socket | Purpose |
|---|---|---|
| POST /auth/setup {} | POST /claim {code,port} | Server consumes setup.code; no workspace needed |
| GET /api/setup/state | unchanged | Initial facts and mode |
| POST /api/setup/profile | unchanged | Exact closed profile |
| POST /api/setup/secrets/reveal, secrets/confirm | unchanged | One-shot text reveal and three-word check |
| POST /api/setup/review, create | unchanged | Core rows, digest and terminal confirmation |
| GET /api/setup/progress?run_id | unchanged | Step statuses / failure / retry facts |
| POST /api/setup/finish, cancel | unchanged | Complete or defer; drop setup scope |

Transport is HTTP/1.1 over `<run>/setup.sock`; ownership, peer UID, private parent and socket mode 0600 are checked. Host is exactly `127.0.0.1:<port>` or `localhost:<port>` for setup, POST Origin must match, and only `X-Setup-Token` authorizes setup APIs. Launch/shutdown are never proxied. No events are authored by the dashboard. Both the contract fake and the actual core SetupServer test this boundary.

Integration: `orch-dashboard open /setup` opens the plain path after port ownership verification. `finish` currently returns `{url:"/"}`; the wizard navigates there and forgets its setup token. This does not mint a normal dashboard session. Progress displays optional waiting text and state failures include the core message and a terminal next step. Runner grants are boolean; core supplies GRANT / AGENTS and the terminal fingerprint.

### Wiki read API (#603)

The server exposes GET `/api/w/{ws}/wiki` (`wiki.list`), `/wiki/page?slug=` (`wiki.get`),
`/wiki/search?q=` (`wiki.search`), and `/wiki/backlinks?slug=` (`wiki.backlinks`), with the latter three paths
relative to `/api/w/{ws}`. Queries forbid extra parameters. Each request checks dashboard/Wiki activation and
workspace membership, including viewers. Pages are regular flat markdown blobs in the workspace repository's
`wiki/` tree at HEAD. Empty repository states are returned by the list route; oversized pages have no content.
Ticket links come from `addons.wiki.pages`, bounded and cached separately per person. No new events or writes.
The mock frontend has not yet been switched to these endpoints.

### Manual acceptance (R3, 2026-10-19)

Existing handoff and click prepare endpoints accept `accept_criterion {key, ac, note?}` → `ac.accept {ref, ac, note?}`, `withdraw_acceptance {key, ac, reason}` → `ac.unaccept {ref, ac, reason}`, and `mark_task_done {key, task}` → `task.mark_done {ref, task}`. Notes/reasons are trimmed, bounded to 1000 characters; reasons are required. Prepared events must be one feedback artifact addition, one matching withdrawal replacement, or one matching task completion without a receipt key, respectively. Host authorization and statement verification remain authoritative.

Criteria pass through host proof, accepted metadata and stale flags without changing coverage calculation. Verify forms display manual context from the ticket document. Optional host SignContent manual annotations are supported, but core currently emits only id/text/checked acceptance rows. Task completion reads lack an actor, so the dashboard says “Marked done, no receipt.” No new event types.

## Hub pages (V3 A7, #859, 23 Oct 2026)

Routes (all owner-only on the host; the server adds no role check of its own): `GET /api/w/<ws>/hub` (host `hub.status`: `mode`, `joined`, `hub_id`, `member`, `link`, `reachable` (null: the host does not say), `not_told`, `owed`, `reseal_needed`, `key_change_pending`, `log_sync_stuck`, `groups` (from the hub, not signed), `snapshot_version`), `GET /hub/share` (`share.rules` + `share.preview {}`: rules with counts, shared tickets by key), `POST /hub/share/preview {add|remove}` (`share.preview`), `POST /hub/command {kind, ...}` (the Terminal line; nothing is sent to the host). Feature `hub` needs the host to list `hub.status`, `share.rules`, `share.preview`. Mock: `?hub=standalone|selective|full|offline|nottold`.
Gaps for core: a hub-reachability field in `hub.status` (offline banner), per-ticket sharing reason and last read time, hub name/company and admin names, number of people losing access on a leave, a prepare hand-off for `hub.join`/`hub.mode`/`hub.leave` (verification fingerprint), `orch hub share apply` (one signed review for several rule changes), `orch hub leave --hub-unreachable`.

### D2: create, optional access, then start (2026-10-28)

`new_sequence` accepts `launch: {harness, mode, model?}` and proposed `agent: {hours}`. Preparation reads host `whoami.runner.now` and the current valid grant: with at least 15 minutes left, it omits the grant; otherwise the grant is the final signed step. The sequence digest includes launch choices. Contract 4 View risk lines cover every sequence step; informational `info_lines` appear only behind “i”.

`POST /api/w/{ws}/agents/start {after_request}` resolves the ticket and launch choices from the host record. Client ticket/spec/idem values are ignored. Incomplete sequences return `sequence.incomplete`; another principal/person cannot use the request. Launch uses the normal host plan and current grant checks, and a request-derived idem plus a durable `after_request` run binding makes retries replay the first run. No new signed event type is introduced.
