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
| Signing surface | `addon-ui/SignConfirm.tsx`, `DecisionSignPrompt.tsx`, `DestructiveConfirm.tsx`, `OptionsConfirm.tsx`, `app/pages/settings/addons/GrantDialog.tsx`, `test/signing-surface.test.tsx` | Signing and confirm dialogs: core's words in the title, covers and confirm button (an addon named "Title (package id)"); every arg that is sent is a core line "Words (key): value" (in the covers, or a "Sends" list above the addon region), never inside the addon's region; the addon's own text (label, sentence, row name, decision question) only in a labelled dashed "From the addon" region, always in full (wrapped, scrolling, never cut). Every addon-controlled or signed string goes through `components/sign/visible.tsx` (`Raw`/`visible`/`plain`: bidi-isolated, control/format characters as `\u{…}`, edge spaces as ␠, empty as `""`). Non-plain values (objects, NaN, Infinity), keys outside `^[A-Za-z][A-Za-z0-9_]{0,31}$` and more than 12 args fail closed. The confirm buttons of the destructive and options dialogs are core's ("Confirm: …", "Continue: …"); option field and choice labels sit in a labelled addon region. Package names must match `^[a-z][a-z0-9-]{0,39}$` and titles must be 1–40 characters without `( ) · :` or invisible characters (`manifestProblem` in `api/addons.ts`; the host refuses the install with 409 `addon.invalid_manifest`, the grant dialog will not sign). The table test covers 19 paths: gate approve, answer, verdict, addon decision, addon sign, options, destructive, start agent, agent grant issue and revoke, skill credential grant, addon install and update, relay connect, pair.confirm and device.remove, member role, gate policy, agent grant length. Not in the table (same SignPrompt pattern, own tests): relay stop, member add/remove. **Explicitly unsigned exception:** terminals `login_shell` (Today's "Log in in the terminal"): owner-only and needs the pty grant, but no signature and no confirm, because it runs nothing: it opens a shell as the connection's `run_as` with the login command typed, and the person presses Enter. The host still appends a `terminal.shell_opened` event (owner as actor). |
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
| POST `/api/tickets/:key/actions` `answer` | `answer` (human, signed) |
| … `approve` / `request_changes` / `verdict` | `approve` / `request-changes` / `verdict` (human, signed). `verdict` carries `source_sha` (the branch head the person saw; 409 `verdict.stale` otherwise) and signs it; `approve {gate: 'code', source_sha}` is the code review (after a pass verdict, the same commit, never an assignee; 409 `gate.stale` / `gate.not_open`); request changes on `code` also voids the verdict |
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
| GET `/api/workspaces/:ws/agents`, `…/agents/activity` | no operation yet — proposal `session.list`, `session.refusals` |
| GET `/api/workspaces/:ws/agents/launch?ticket&mode&harness&where` | no operation yet — proposal `session.preview` (core-computed facts for the start dialog) |
| (start/stop run through the start-agent addon actions) | no operation yet — proposal `session.start` / `session.stop` (human; needs `spawn_agent`, a grant, a server-issued single-use confirmation) |
| GET/POST `/api/workspaces/:ws/preview/mandates` | **PREVIEW ONLY — not part of the contract.** Served by the mock for the non-functional mandates preview (M1, `docs/concept-mandates.md` Step 1); nothing is signed. A host implements nothing here until core specifies mandates (D62 draft, PR #340); the shape will change. |
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
| GET `/api/addons`, `/api/workspaces/:ws/addons`, `…/addon-catalog` | `addon …` (list) |
| POST `/api/workspaces/:ws/addons/:name` `{op}`: `install`, `grant`, `update`, `enable`, `disable`, `uninstall` | `addon …` (grant/update/install-with-grant are signed: format `addon.granted`) |
| GET `/api/workspaces/:ws/addon-decisions` | no operation yet — proposal: part of `inbox` (addon decisions core renders) |
| GET `/api/workspaces/:ws/addons/:name/state[?ticket=KEY]` | no operation yet — proposal `addon.state` (the addon's view for this person; per-ticket for panels) |
| POST `/api/workspaces/:ws/addons/:name/actions/:id` | the addon's own command group (format §8 `cli`), run by the host; `confirm: 'sign'` and `decision: true` actions are signed by core (events `addon.action_signed`, `addon.decided`) |

The host requires core's `confirmed: true` on `confirm: 'sign'`, `'destructive'`, `'options'`, `'spawn_agent'` and decision
actions (409 `confirm.required`); a signed action carries at most 12 plain values (400 `validation`), and
`addon.action_signed` records exactly the signed args (uncut, without `confirmed`).

**Proposal for the addon contract: decision `terms`** (F3 workspace links, 2026-10-10). An `AddonDecision` may carry
`terms: Record<string, string | number>`: what answering authorises (a link's peer, comparison code, carrier, scopes
each way, expiry). Same limits as signed args: at most 12 plain finite values under `^[A-Za-z][A-Za-z0-9_]{0,31}$` keys;
anything else fails closed (core does not offer the decision; `validTerms` in `api/addons.ts`, applied in
`openDecisions`). Core shows each term in the decision's signing covers as its own line "Words (key): value" (core
humanises the key; the value through `visible.tsx` `Raw`, in full), posts them with the answer (`decisionBody`), and the
host refuses the answer with 409 `decision.closed` ("…or its terms changed", hint "Reopen it and check the terms again")
unless they equal the decision's terms now (`sameTerms`: same keys, values and types). `addon.decided` records them. The
id still binds the decision; terms make what it binds legible. On an addon page, a decision node's primary option is
disabled while a form on that page that names it (`guards: <decision id>`, with `cancel`) holds unsaved edits
("Unsaved changes to the terms: save or cancel them before you accept."); other options stay.

The host refuses package names that are core namespaces (`addon`, `gate`, `role`, `policy`, `edit`, `projection`,
`restore`, … : `CORE_EVENT_NAMESPACES` in `mocks/addons/registry.ts`; install answers 409 `addon.reserved_name`) and
accepts only `<name>.<verb>` records from an addon.

Addon action ids the mock implements (manifest `actions` sets roles; default member): publish `share`, `share_once`,
`copy_link`, `extend`, `revoke`, `start`, `stop`, `logs`, `redeploy`, `decide`; github `import`, `refresh`,
`approve`, `open`; estimate `set`; wiki `open`, `search`, `clear_search`, `close`, `edit`, `done`, `create`, `save`,
`link`; terminals `open`, `new`, `close`, `open_ticket`, `start`, `resume`, `login_shell` (owner only plus the pty grant, else 403 / 409 `terminals.no_pty`; body is only `{connection}`, never a command: the host takes the command from that connection's `login_hint` (404 unknown connection, 400 no login); the shell runs as the connection's `run_as`; the hint is written to the pty once on first attach with control characters and newlines stripped and no trailing CR, so nothing runs until the person presses Enter; the session view carries `prefill` and `run_as`; appends `terminal.shell_opened {connection, run_as, session}` by the owner; unsigned by design, see the signing surface row); start-agent `configure`, `start`, `stop`;
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
  `verdict.given` (with `source_sha`; a charter verdict adds `via`, `charter`, `charter_signed_by`), `agent.refused`,
  `branch.pushed` (an agent pushed `sha` to the ticket branch). `gate.approved` may name gate `code` (the code
  review, with `source_sha`).
- Provisional, workspace log: `ticket.discarded`, `workspace.renamed`, `member.removed`, `view.saved`,
  `view.deleted`, `grant.issued`, `grant.revoked`, `agent.started`, `agent.stopped`, `addon.installed`,
  `addon.enabled`, `addon.disabled`, `addon.updated`, `addon.uninstalled`, `addon.settings_saved`, `addon.decided`,
  `addon.action_signed`, `connection.checked`, `skill.credentials_granted`, `relay.connected`, `relay.stopped`,
  `device.paired`, `device.removed`, `epoch.rotated`, `workspace.grant_hours_set`, `terminal.shell_opened` (owner/maintainer-visible in Activity).
- Provisional, addon (`<addon>.<verb>` by `{kind:'addon'}`, format T11): `publish.shared`, `publish.revoked`,
  `publish.decided`, `github.imported`, `github.pr_linked`, `estimate.set`, `usage.recorded`, `wiki.linked`,
  `quick.made_ticket`, `records.committed`, `records.pushed`, `records.pulled`, `drop.shared`, `drop.claimed`,
  `drop.revoked`, `drop.removed`, `drop.extended`, `factory.paused`, `factory.resumed`, `factory.permit_granted`,
  `factory.permit_refused`, `land.queued`, `land.attempt`, `land.dequeued`, `land.resolved`.
- Not written by the mock: `artifact.replaced`, `edit.external`, `projection.repaired`, `restore`.

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
  failed attempt and the resolution records and writes the reason), never by the addon. `main` is refused as a target
  (D33, normalised names). Decided (10 Oct): the verdict signs the branch head (`source_sha`), the land worker uses
  that commit, new commits void the verdict, and the opt-in `code` gate (when on) signs the same commit.
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
cd addons/dashboard
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
tests after each small change; full suite, typecheck and build at the end of a group. Commit only `addons/dashboard`
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
- The land worker's real push is a fast-forward with a lease on the target SHA, and `main` must be refused at push
  time too (see DECISIONS-LOG "N9 review fixes").
- Addon settings drawer: a save by someone else while you edit resets your edits (needs an "Updated elsewhere" design).
- The simulated Claude welcome box clips in a very narrow terminal (allowed: terminal content).
- **Mandates are a preview only (M1), not part of the contract.** Agents → Mandates, the shell's mandate banner,
  Today's "Decided for you" and Demo data → "Preview: mandates" show how the owner-approved Step 1 pilot
  (`docs/concept-mandates.md`) would look. Nothing signs; the preview endpoint, its types (`src/api/mandatesPreview.ts`)
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
