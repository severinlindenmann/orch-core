# Handover: orch Mission Control mockup

State on 8 Oct 2026, end of **iteration 1 of 3**. Branch `feat/dashboard-mockup` (off `develop`), draft PR into
`develop`. This file tells the next agent where the draft stands, what was decided, and what comes next.

## Goal

A **complete, clickable frontend mockup** of the orch v2 dashboard addon (Mission Control), running on simulated
JSON data with working, simulated buttons. When the backend exists, swap the mock transport for real `fetch` calls
to the FastAPI host. **No component changes should be needed.**

- Desktop and small desktop only (≥1024 px); no mobile.
- Every element that comes from an addon is marked with a small orange **A** badge.

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
| Order | It1: shell, Today, Board, Ticket (**done**). It2: tickets list and search, full ⌘K, New ticket, Agents (claims, leases, grants), workspace settings (members and roles, gate policies, addon manager with capability grants), full workspace switcher. It3: every addon (see below). The owner reviews after each iteration. |

## Source of the data model

The mock follows the decided orch v2 ticket format and agent interface:

- `docs/architecture/orch-v2-ticket-format.md` on `main`. Read it with
  `git show origin/main:docs/architecture/orch-v2-ticket-format.md`.
  - §3 `ticket.json`, §5 events, §7 ticket document, §9 visibility, §10 agent interface.
- `docs/architecture/orch-v2.md` and `orch-v2-carryover.md` on `main`: the decisions D1–D41 and the carry-over list.
  This dashboard is the "frontend addon".

The demo data is fictional: "Acme energy data" (DEMO), Internal (INT) and Client VM (CLI). The people are Severin
(owner, `p_sev`), Mara (maintainer, `p_mara`) and Tom (viewer, `p_tom`). The richest example ticket is **DEMO-0043**.

## What exists (iteration 1)

| Area | Files | Contents |
|---|---|---|
| Brand and tokens | `src/brand/OrbitMark.tsx`, `src/styles/tokens.css` | Dark tokens wired into Tailwind `@theme` and shadcn. Brand teal is `brand` / `on-brand`; shadcn's `accent` is the neutral hover. |
| API layer | `src/api/types.ts`, `client.ts`, `transport.ts` | Typed client over a `Transport`: `createMockTransport` (used) and `createFetchTransport` (ready for later). |
| Mock | `src/mocks/router.ts`, `store.ts`, `derive.ts`, `fixtures/` | Endpoints listed below. State is derived from events, as in the spec. Mock "now" is 2026-10-09 11:30Z. |
| Shell | `src/app/shell/` | Sidebar (wide or rail), topbar with breadcrumb, ⌘K palette (`CommandPalette.tsx`), New ticket placeholder, "Demo data" pill with reset, viewer switch (Severin / Mara / Tom). |
| Addon UI | `src/addon-ui/` | `AddonBadge`, `AddonFrame`, slot registry (`useSlot`, `AddonSlotStack`, `AddonContributionView` with `compact`), node renderer, addon page route `addon/$name/$page`. |
| Today | `src/app/pages/today/` | Decision cards (answer, approve with a sign dialog and simulated Touch ID, verdict, addon decisions), agents at work with subagent leases, `today.card` addon tiles, "Recently". There is also a read-only view for viewers. |
| Board | `src/app/pages/board/` | Status columns with dnd-kit (keyboard too); "done" is refused because only a verdict reaches it. Filters, a list view, `board.card_field` (estimate points), and the `board.lane` GitHub issues column with Import. |
| Ticket | `src/app/pages/ticket/` | Header, people, claim with leases, gates strip (policy, approvals, presence, sig ok, invalidated reason). Tabs: Overview, Acceptance & tasks (evidence verified vs agent-asserted, receipts), Questions, Artifacts (log, CSV and code viewers), History (timeline plus section word diff), Raw. The rail holds needs-you, details, branches/PRs and `ticket.panel` addon panels. There are restricted and not-found states. |

**Mock endpoints:**

- GET `/api/me`, `/api/workspaces`, `/api/addons`, `/api/addons/decisions`
- GET `/api/workspaces/:ws/today` (needs-you per viewer, `read_only_open` for viewers), `/tickets` (status, q, type, parent),
  `/agents`
- GET `/api/tickets/:key`, `/api/tickets/:key/events?since=`
- POST `/api/tickets/:key/actions` with answer, approve, request_changes, verdict, comment, ask, claim, release, set_status
- POST `/api/addons/:name/actions/:id` (publish decide/share, github import, estimate set)
- POST `/api/dev/reset`, `/api/dev/viewer`

**Addons in the fixtures** (`src/mocks/fixtures/addons.json`):

- **publish:** a page and a Today card; a ticket panel and a decision.
- **github:** a page and a Today card; a board lane and a ticket panel.
- **usage:** a page with a chart and a Today card; a ticket panel.
- **terminals:** a page listing sessions only.
- **wiki:** a page and a ticket panel.
- **estimate:** a card field, a ticket panel form and a settings form.

## How to run, test and preview

```
cd addons/dashboard
npm install
npm run dev            # local dev server
npx vitest run         # 34 tests (about 10 s)
npx tsc --noEmit -p tsconfig.app.json
npm run build          # dist/
```

**Working rules** (the owner's): Opus manager, Sonnet implementers, and an Opus review only for security-relevant
work. Run **targeted tests after each small change** (`npx vitest run src/app/pages/<page>`). Run the full suite and
the build only at the end of a group. Commit only `addons/dashboard` paths, and never commit `.design-drafts/`.

**Preview link for the owner:** https://claude.ai/artifact/MQWNJj1NZJ9CGCCibHCnmx (version 2). To update it:

1. Run `npm run build`.
2. Copy `dist/assets` into a preview folder under the working directory.
3. The preview page is a tiny `index.html`:
   - a `<title>`;
   - the Google Fonts link;
   - the built CSS;
   - a `<style>` with the dark background;
   - a script adding the `dark` class;
   - `<div id="root">`;
   - the built module script.
4. **Escape U+FFFD** in the main JS bundle before publishing: the publisher refuses literal `�`. Replace it with
   the JS escape `�`; the occurrences are inside template strings, so this is safe.
5. Publish to the same URL. Pass the new hashed files in `files` and `null` for the old ones.

## Known gaps and notes

- **One flaky test:** in about 1 of 4 full runs, one test fails. Most likely the mock latency (120–300 ms) races
  testing-library's 1 s `findBy` timeout under parallel load. Fix it by setting latency to 0 in tests (e.g. an env
  check in `mocks/router.ts`) or by raising `asyncUtilTimeout`.
- Bundle: a main chunk over 500 kB triggers a warning. Code-split routes and shiki later.
- lucide 1.x has no brand icons, so the GitHub manifest icon maps to `GitBranch`.
- "New ticket" is a placeholder dialog (iteration 2). The Tickets, Agents and Settings routes are placeholders
  (iteration 2).
- Terminals is only a list. The real xterm.js terminal over a fake PTY comes in iteration 3.
- Approval cards show the per-gate hash from the mock. The "Touch ID" is a 600 ms simulation.

## Next: iteration 2 (agreed scope)

1. **Tickets** list and search: a table with filters, saved views, and full-text search via `/tickets?q=`.
2. **⌘K**, the full palette: tickets, actions on the current ticket, navigation, addon commands (with badge), recent
   items.
3. **New ticket** flow: type, title, the sections the type needs (spec §4), labels, parent, people. It creates a
   backlog ticket in the mock.
4. **Agents** page:
   - active sessions per person, claims and task leases, standing grants (`orch grant`) with expiry;
   - "revoke grant" (human-only, signed);
   - recent agent events and refusals.
5. **Workspace settings:**
   - members and roles (owner, maintainer, member, viewer);
   - gate policy defaults;
   - the **addon manager**: install, enable or disable, capabilities, and a signed capability grant per version, with
     a re-grant needed on update;
   - per-addon settings forms from JSON Schema (slot `settings`);
   - workspace identity (UUID, prefix), the relay connection (placeholder), danger zone.
6. **Workspace switcher**, the full version: cross-workspace needs-you, and switching keeps the page.

Then **iteration 3**, all addons, each marked with the A: publish (apps and shares), github (reviews and the issues
lane), terminals (xterm.js), usage, wiki, estimate, worktrees, quick tasks, records, activity, widgets, start agent,
guide, model-routing, AI Factory (Phase 2 preview), schedules (later).

**Start iteration 2 by asking the owner whether anything on the three pages should change.** The owner gives
feedback step by step and prefers one question at a time, with a recommendation.
