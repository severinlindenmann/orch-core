# Handover: orch Mission Control mockup

State on 9 Oct 2026, **after the complete mockup** (iterations 1–3, the UX review rounds and the owner's wave-3
input). Branch `feat/dashboard-mockup`; PR #330 (iteration 1 and 2, up to 906a87b) was merged into `develop`
(9381ac6) and the remote branch was deleted. This file tells the next agent where the mockup stands, what the backend
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
| Build | Real code, not a design canvas: React 19, Vite 8, TypeScript, shadcn/ui (Radix), Tailwind v4, TanStack Router (memory history), TanStack Query, lucide. Exact pins are in `package.json`. |
| Mock | An **in-process mock API** (`src/mocks/`: router, store, derive, fixtures). There is **no MSW or service worker**, because the claude.ai preview viewer blocks service workers. Buttons really change the mock state; the store persists to localStorage. |
| Addon UI | Addons describe their UI as **declarative JSON nodes** (stack, stat, kv, list, table, markdown, code, chart, form, button, link), validated with zod and rendered by core inside `AddonFrame`. Slots: `nav`, `today.card`, `ticket.panel`, `board.lane`, `board.card_field`, `settings`. Rich addon pages later go into a sandboxed iframe. |
| Core-only | Approvals, answers, verdicts and addon *decisions* are always rendered and "signed" by core, never by an addon node. |
| Order | It1: shell, Today, Board, Ticket (**done**). It2: tickets list and search, full ⌘K, New ticket, Agents (claims, leases, grants), workspace settings (members and roles, gate policies, addon manager with capability grants), full workspace switcher. It3: every addon. All three are done (9 Oct). The owner reviews after each iteration. |

**Later owner input (9 Oct), all built:** wave 3 A–K (terminal dock, group by epic, addon pages with tabs, usage by
model, wiki, the widget catalog, addon settings in a drawer, New ticket overlay and Quick ticket, Relay & devices,
Drop, Artifacts, motion), wave 3b (D53 Landing, D55–D57 Skills, Connections and simple auth) and wave 3c (every page
fits a 13" notebook with the right dock). Controller rulings that refine the table above (not reopening it): addon
markers once per surface for dense items (board chips, table cells), addon install is one signed "Grant and turn on".

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
| Tests | `**/*.test.ts(x)`, `test/` | About 1,750 vitest tests (jsdom): contract, visibility sweep, orange guard, a11y smoke, per page and per addon. `scripts/layout-guard.mjs` for real layout (below). |

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
| same, `gate.policy` | no operation yet — proposal `policy.set` (format event `policy.changed`; signed) |
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
| … `approve` / `request_changes` / `verdict` | `approve` / `request-changes` / `verdict` (human, signed) |
| … `ask` | `ask` |
| … `comment` | no operation yet — proposal `comment` (closest: `log`, which is the agent's) |
| … `claim` / `release` | `claim` / `release` (agents only; the dashboard never offers them) |
| … `set_status` | no operation yet — proposal `move` (owners/maintainers; `done` refused: only a verdict) |
| … `add_label` | `set REF labels=…` |
| GET/POST `/api/workspaces/:ws/views`, POST `…/views/:id/delete` | no operation yet — proposal `view.save` / `view.delete` (dashboard-only data) |
| GET `/api/workspaces/:ws/artifacts?kind&ticket&by&since&q&page` | `artifact list` is per ticket; the workspace-wide, paged listing is a proposal `artifact.search` |

**Agents and grants**

| Endpoint | Operation |
|---|---|
| GET `/api/workspaces/:ws/agents`, `…/agents/activity` | no operation yet — proposal `session.list`, `session.refusals` |
| GET `/api/workspaces/:ws/agents/launch?ticket&mode&harness&where` | no operation yet — proposal `session.preview` (core-computed facts for the start dialog) |
| (start/stop run through the start-agent addon actions) | no operation yet — proposal `session.start` / `session.stop` (human; needs `spawn_agent`, a grant, a server-issued single-use confirmation) |
| GET/POST `/api/workspaces/:ws/grants`, POST `…/grants/:id/revoke` | `grant` (human, signed; revoke stops its sessions) |

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

Addon action ids the mock implements (manifest `actions` sets roles; default member): publish `share`, `share_once`,
`copy_link`, `extend`, `revoke`, `start`, `stop`, `logs`, `redeploy`, `decide`; github `import`, `refresh`,
`approve`, `open`; estimate `set`; wiki `open`, `search`, `clear_search`, `close`, `edit`, `done`, `create`, `save`,
`link`; terminals `open`, `new`, `close`, `open_ticket`, `start`, `resume`; start-agent `configure`, `start`, `stop`;
guide `open`; worktrees `filter`, `add`, `remove`, `open_terminal`; quick `add`, `claim`, `close`, `make_ticket`,
`decide`; records `commit`, `push`, `pull`; activity `apply`, `view_timeline`, `view_ticket`, `show_new`,
`clear_filters`, `show_older`; models `escalate`; factory `permit`, `pause`, `resume`, `watch`, `stop_watching`;
schedules `arm`, `disarm`, `run_now`, `open_run`, `finding`; drop `share`, `claim`, `download`, `remove`, `extend`,
`revoke`; land `enqueue`, `dequeue`, `resolve`, `mark_resolved`, `run_worker`, `stop_worker`, `open_checks`; and
`save_settings` (owner) on every addon with settings.

**Mock only (not part of the contract):** POST `/api/dev/reset {dataset?}`, GET `/api/dev/dataset`, POST
`/api/dev/viewer`, POST `/api/dev/relay/:ws` (simulate a dropped link / a phone scanning).

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
  `task.skipped`, `log.added`, `comment.added`, `question.asked`, `question.answered`, `gate.changes_requested`,
  `gate.invalidated` (also core's landing void), `verdict.given`, `agent.refused`.
- Provisional, workspace log: `ticket.discarded`, `workspace.renamed`, `member.removed`, `view.saved`,
  `view.deleted`, `grant.issued`, `grant.revoked`, `agent.started`, `agent.stopped`, `addon.installed`,
  `addon.enabled`, `addon.disabled`, `addon.updated`, `addon.uninstalled`, `addon.settings_saved`, `addon.decided`,
  `addon.action_signed`, `connection.checked`, `skill.credentials_granted`, `relay.connected`, `relay.stopped`,
  `device.paired`, `device.removed`, `epoch.rotated`.
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
  (D33, normalised names). Open question for the owner: the bound approval is the **verify** approval (the verdict).
- **D54.** No relay-hosted page anywhere; Drop links are app-only (open owner question about a `/d/` fallback).

## How to run, test and preview

```
cd addons/dashboard
npm install
npm run dev                      # dev server (the session uses 5180 live and 5181 as a stable snapshot)
npx vitest run <path>            # targeted tests after each change
npx vitest run                   # full suite, ~1,750 tests, a few minutes; once per phase
npm run typecheck
npm run build                    # dist/
npm run layout:guard             # 13" notebook check (below)
```

**Layout guard (`npm run layout:guard`, `scripts/layout-guard.mjs`).** The owner's minimum: every page works on a 13"
notebook in full screen with the terminal docked on the right, without sideways scrolling. jsdom cannot lay out, so
the guard drives headless Chrome over the DevTools protocol (plain node, no Playwright) against a running dev build
(`npx vite --port 5201 --strictPort --host 127.0.0.1`, or `--url <base>`). It moves between routes through
`window.__orchRouter` (dev builds only) and checks every core page, every addon page (also under "More addons"),
every Settings tab and addon drawer, every tab of DEMO-0043, `/tickets/new`, the New ticket overlay and the Review
tour sheet, at 1440×900 and 1470×956, right dock at min / default / max width, sidebar wide and rail, Normal and Busy
day. It fails (exit 1) on document or element overflow outside the allowed scrollers (terminal, `pre`/`code`, tab
rows, `data-scroll-x`), on a cut-off button/link/tab, or when a step did not open; silent clipping is a warning.
Options: `--quick` (one configuration), `--docks min,max`, `--dataset busy`, `--only <regex>`, `--shots <dir>`,
`--selftest` (plants overflow and breaks steps on purpose to prove it fails). Last run: 0 failing.

**Working rules** (the owner's): Opus manager, Sonnet implementers, Opus review for security-relevant work. Targeted
tests after each small change; full suite, typecheck and build at the end of a group. Commit only `addons/dashboard`
paths; never commit `.design-drafts/`. No realistic-looking secrets in seeds (GitHub push protection).

**Preview for the owner.** Hosted preview: (added on publish). Local: http://127.0.0.1:5180/ (live) and
http://127.0.0.1:5181/ (stable snapshot). To publish a new version:

1. `npm run build`.
2. Copy `dist/assets` into a preview folder in the session scratchpad.
3. Write the small `index.html`: `<title>`, the Google Fonts link, the built CSS, a `<style>` with the dark
   background, a script adding the `dark` class, `<div id="root">`, the built module script.
4. In **every** `.js` file (lazy chunks too) replace each literal U+FFFD with the JS escape `�` and escape C0/C1
   control characters (the xterm chunk has a literal ESC); the publisher refuses them. They sit inside strings, so
   escaping is safe.
5. Publish to the same artifact URL, passing the new hashed files in `files` and `null` for the old ones.

## Known gaps and notes

- Memory routing: the app always starts on Today; a pasted deep link does not open its page (on purpose: it also runs
  in the sandboxed viewer).
- `confirmed` (core's confirm flag on spawn/sign/decision actions) is a plain boolean in the mock. A real host needs
  a server-issued, single-use confirmation bound to the person, the action and the arguments.
- The start dialog's facts come from core's `agents/launch` preview; a real host renders them from its own resolver.
- The land worker's real push is a fast-forward with a lease on the target SHA, and `main` must be refused at push
  time too (see DECISIONS-LOG "N9 review fixes").
- Addon settings drawer: a save by someone else while you edit resets your edits (needs an "Updated elsewhere" design).
- Parked engineering minors: a duplicate import in the palette; a dock open-request for a session that never appears
  is not cleared. R4 (running in parallel) takes the rest of the parked list.
- The simulated Claude welcome box clips in a very narrow terminal (allowed: terminal content).

## Next

1. **Finish round R4** (ticket page density, widgets, busy richness, usage; worktree `ux/r4`) and merge it; the
   layout guard (`--docks min,max`) must stay at 0 failing.
2. **Owner review** with [REVIEW.md](REVIEW.md) and the in-app Review tour; answer its open questions.
3. **Next PR:** merge `origin/develop` into `feat/dashboard-mockup` (the remote branch was deleted after #330),
   push, open a PR against `develop`, ping the orch v2 build session (orch-4e) when it is open. Merge only on green CI.
4. **Backend swap**, once the P2 host exposes the operation registry over its socket:
   - implement the operations named in "Backend contract", and the proposals the owner accepts;
   - point `client.ts` at a transport over the host (`createFetchTransport` or a socket transport with the same
     `Transport` shape); remove `/api/dev/*` and the review tour;
   - settle event names with issue #338 and rename the provisional ones in the mock and `describeEvent`;
   - move the rules the mock enforces (permissions, visibility, gate policy, addon roles, refusal codes) into the
     host; the UI keeps them only as convenience.
