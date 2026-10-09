# Complete Mission Control Mockup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the clickable orch v2 Mission Control mockup: every core page from iteration 2 and every addon from
iteration 3, on simulated data, then test the whole app for UX and UI quality so the owner can validate it in one
sitting.

**Architecture:** Everything stays in `addons/dashboard`. The in-process mock API (`src/mocks/`) grows a
workspace-level event log, ticket creation, an addon-module registry with per-addon state and a small live
simulator that plays scripted agent runs. Pages talk only to `src/api/client.ts`, so swapping in
`createFetchTransport` later needs no component changes. Addons keep describing their UI as declarative JSON nodes
rendered by core; the node set gains a few closed types (item actions, `alert`, `progress`, `frame`, `terminal`).

**Tech Stack:** React 19, Vite 8, TypeScript 7, shadcn/ui (Radix), Tailwind v4, TanStack Router (memory history) and
Query, zod, cmdk, dnd-kit, recharts, shiki, rjsf, vitest + testing-library. New in this plan: `@xterm/xterm` (Task 21)
and `axe-core` as a dev dependency (Task 31).

**Spec:** `addons/dashboard/HANDOVER.md` (decisions, iteration scope) and, on `main`,
`docs/architecture/orch-v2-ticket-format.md` (§3 ticket.json, §4 body sections per type, §5 events, §8 addons,
§9 visibility, §10 agent interface), `orch-v2.md` and `orch-v2-carryover.md`. Read a spec file with
`git show origin/main:docs/architecture/<file>`. v1 behaviour of the iteration-3 addons is on `main` under
`plugins/orch-core/docs/` (quick-tasks.md, schedules.md, widgets.md, factory.md) and
`plugins/orch-core/addons/model-routing/README.md`.

## Global Constraints

- All work under `addons/dashboard/`. Commit only `addons/dashboard` paths. Never commit `.design-drafts/`.
- Branch `feat/dashboard-mockup`; one commit per task; push at the end of each phase (updates the draft PR).
- Desktop only, ≥ 1024 px. No mobile layouts.
- **Dark only.** No light theme, no theme toggle.
- **Orange is reserved for addons.** Every addon contribution gets `AddonBadge` (orange "A") and `AddonFrame`
  (orange hairline). Never use orange for anything else (status, warnings, danger: use the existing tokens).
- Brand: "A · Orbit", teal `brand` token `#2fc6a3`, Geist / Geist Mono. Use tokens from `src/styles/tokens.css`,
  never raw hex in components.
- **Approvals, answers, verdicts, grants, capability grants, revokes and addon decisions are rendered and signed by
  core** (`SignDialog`), never by an addon node.
- `pty` is never granted to agents; agents never enable addons.
- No MSW, no service worker. The mock is in-process.
- Components never import from `src/mocks/` directly; only `src/api/client.ts` does.
- Pinned versions: add dependencies with an exact version (no `^`), as in `package.json`.
- Mock "now" is `2026-10-09T11:30:00Z` (`MOCK_EPOCH`).
- People: Severin `p_sev` (owner), Mara `p_mara` (maintainer), Tom `p_tom` (viewer). Workspaces DEMO, INT, CLI.
- Tests: targeted after each change (`npx vitest run <path>`); the full suite, `npm run typecheck` and
  `npm run build` only at the end of a phase.
- Working rules: Opus manager, Sonnet implementers, Opus reviewer only for tasks marked **[security review]**.
- **Autonomous run:** the owner is away. Do not stop to ask. Make the call, and record every non-trivial product
  decision in `addons/dashboard/DECISIONS-LOG.md` (date, decision, why, how to revert) for the owner's review.
  Decisions in the HANDOVER table are not reopened.

## Review Focus

1. **Stored demo state from an older build.** A browser holding iteration-1 `localStorage` must load the new build
   without a crash: unknown or old-shape data is discarded and the seed is used (Task 1, test "discards v1 storage").
2. **The viewer (Tom) on every new page.** Write controls are hidden or disabled with a reason, and a 403 from the mock
   becomes a toast, never a blank page (Tasks 6, 9, 10, 11, 12: each has a "viewer" test).
3. **A disabled or uninstalled addon.** Its nav entry, slots, palette commands, Today cards and board lanes vanish;
   its ticket data shows as "inactive"; a direct `/addon/<name>/<page>` URL shows "not enabled" (Task 13 tests).
4. **Switching workspace while on a page that does not exist there** (a ticket, an addon page of a disabled addon)
   lands on the nearest sensible page instead of an error (Task 15 tests).
5. **1024 px with the wide sidebar.** Tables, forms and the terminal never cause horizontal page scroll; long titles
   truncate with a tooltip (Task 31 layout test, Task 32 walkthrough).

---

## File Structure

New and changed files, by responsibility:

```
src/test/renderApp.tsx               shared test harness: fresh store, zero latency, router + query
src/api/types.ts                     + workspace events, grants, settings, saved views, new-ticket, addon state
src/api/client.ts                    + new endpoints; latency off under test
src/mocks/store.ts                   slimmed: delegates to the modules below
src/mocks/persist.ts                 versioned localStorage (v2), discard on mismatch
src/mocks/workspace-log.ts           workspace event log + derived members/gates/addons/grants
src/mocks/tickets-create.ts          new-ticket validation + ticket.created
src/mocks/sections.ts                spec §4 table: sections per ticket type (shared with the UI)
src/mocks/addons/registry.ts         MockAddon interface + registry
src/mocks/addons/<name>.ts           one module per addon: state, actions
src/mocks/sim.ts                     live simulator: scripted agent runs over time
src/mocks/fixtures/*.json            + catalog, grants, views, refusals, addon state seeds
src/addon-ui/nodes.ts                + item actions, alert, progress, frame, terminal
src/addon-ui/AddonNode.tsx           render the new node types
src/addon-ui/FrameNode.tsx           sandboxed iframe node
src/addon-ui/slots.tsx               + `addon` state in the slot context
src/app/live.ts                      live updates: poll the workspace cursor, invalidate queries
src/app/pages/tickets/               Tickets list, filters, saved views
src/app/pages/new-ticket/            New ticket page
src/app/pages/agents/                Agents page
src/app/pages/settings/              Settings: general, members, gates, addons, addon settings
src/app/shell/CommandPalette.tsx     full palette (split into palette/ if > 300 lines)
src/app/shell/WorkspaceSwitcher.tsx  full switcher
src/app/terminal/                    xterm view + fake PTY (core-rendered, `pty` capability)
src/app/review/ReviewTour.tsx        owner's review checklist (dev pill)
DECISIONS-LOG.md                     autonomous decisions for the owner
REVIEW.md                            how to review the mockup, scenario by scenario
```

---

# Phase 0: Foundations

### Task 0: Shared test harness and the flaky test

The flaky test is the 120–300 ms mock latency racing testing-library's 1 s `findBy` timeout under parallel load. UI
tests use the app-wide `api` singleton, which has latency on and shares one store across a file.

**Files:**
- Create: `src/test/renderApp.tsx`
- Modify: `src/api/client.ts` (singleton), `src/mocks/store.ts` (export `resetMockStoreForTests`)
- Modify: `src/app/shell/shell.test.tsx`, `src/app/pages/{today,board,ticket}/*.test.tsx` (use the harness)
- Create: `DECISIONS-LOG.md`

**Interfaces:**
- Produces: `renderApp(path?: string, opts?: { viewer?: 'p_sev' | 'p_mara' | 'p_tom' }): RenderResult & { user: UserEvent }`
  in `src/test/renderApp.tsx`. Every later UI test uses it.
- Produces: `mockStore` (the singleton `MockStore` behind `api`) exported from `src/api/client.ts` **for tests only**,
  and `resetMockStoreForTests(): void`.

- [ ] **Step 1: Write the failing test** in `src/test/renderApp.test.tsx`

```tsx
import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { renderApp } from './renderApp'
import { api } from '@/api/client'

describe('renderApp harness', () => {
  it('answers without simulated latency', async () => {
    const t0 = performance.now()
    await api.getMe()
    expect(performance.now() - t0).toBeLessThan(50)
  })
  it('starts every render from the seed and the chosen viewer', async () => {
    await api.setViewer('p_tom')
    renderApp('/', { viewer: 'p_sev' })
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument()
    expect((await api.getMe()).person).toBe('p_sev')
  })
})
```

- [ ] **Step 2: Run** `npx vitest run src/test/renderApp.test.tsx` — expected FAIL (module missing).

- [ ] **Step 3: Implement.** In `src/api/client.ts`:

```ts
const isTest = import.meta.env.MODE === 'test'
/** Exposed for tests only (renderApp resets it). */
export const mockStore = createMockStore({ persist: !isTest })
export const api: Api = createApi(createMockTransport(mockStore, { latency: !isTest }))
export function resetMockStoreForTests() { mockStore.reset() }
```

`src/test/renderApp.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider } from '@tanstack/react-router'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { mockStore, resetMockStoreForTests } from '@/api/client'
import { createAppRouter } from '@/app/router'

export function renderApp(path = '/', opts: { viewer?: string } = {}) {
  resetMockStoreForTests()
  if (opts.viewer) mockStore.setViewer(opts.viewer)
  try { localStorage.clear() } catch { /* jsdom */ }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const user = userEvent.setup()
  const r = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createAppRouter(path)} />
    </QueryClientProvider>,
  )
  return { ...r, user }
}
```

Replace the local `renderApp` copies in the four existing UI test files with this import. Create `DECISIONS-LOG.md`
with a header and the first entry: "Latency is off under `MODE === 'test'`; UI tests share `renderApp`."

- [ ] **Step 4: Run** `npx vitest run` five times in a row (`for i in 1 2 3 4 5; do npx vitest run || break; done`).
  Expected: 36 tests pass in all five runs.
- [ ] **Step 5: Commit** `test: shared renderApp harness, zero mock latency in tests (fixes flaky test)`

### Task 1: Versioned persistence and the workspace event log

Iteration 2 changes workspace state (members, gate policies, addons, grants, saved views, new tickets). Like tickets,
that state is derived from events, here a **workspace log** per workspace.

**Files:**
- Create: `src/mocks/persist.ts`, `src/mocks/workspace-log.ts`, `src/mocks/workspace-log.test.ts`
- Modify: `src/mocks/store.ts` (use both), `src/api/types.ts`

**Interfaces:**
- Produces in `src/api/types.ts`:

```ts
export type WorkspaceEventType =
  | 'member.added' | 'member.role_changed' | 'member.removed'
  | 'gate.policy_set'
  | 'addon.installed' | 'addon.granted' | 'addon.enabled' | 'addon.disabled' | 'addon.updated' | 'addon.uninstalled'
  | 'addon.settings_saved'
  | 'grant.issued' | 'grant.revoked'
  | 'view.saved' | 'view.deleted'
  | 'workspace.renamed'
export interface WorkspaceEvent { v: 2; id: string; seq: number; at: string; type: WorkspaceEventType; actor: Actor; [k: string]: unknown }
export interface GrantInfo {
  id: string; person: string; scope: 'all' | 'ci'; issued_at: string; until: string
  revoked: { at: string; by: string } | null
  sessions: string[] // agent sessions currently using it
}
```

- Produces in `src/mocks/persist.ts`:

```ts
export const STORAGE_KEY = 'orch-mock-v2'
export interface PersistedV2 {
  v: 2
  ticketEvents: Record<string, OrchEvent[]>   // appended after the seed, per ticket key
  created: Record<string, { ws: string; def: TicketDefinition; body: BodySections }>
  wsEvents: Record<string, WorkspaceEvent[]>  // appended after the seed, per workspace id
  addonState: Record<string, Record<string, unknown>> // `${ws}/${addon}` -> state
  viewer?: string
}
export function loadPersisted(): PersistedV2 | null   // null when absent, unparsable, or v !== 2
export function savePersisted(p: PersistedV2): void   // swallows storage errors
```

- Produces on `MockStore`: `appendWs(wsId, { type, ...fields }): WorkspaceEvent`, `wsEventsOf(wsId)`,
  `grants(wsId): GrantInfo[]`. `workspaceList()` derives `members`, `gates` and `addons` by folding the seed plus
  the log.

- [ ] **Step 1: Write failing tests** `src/mocks/workspace-log.test.ts`

```ts
import { beforeEach, describe, expect, it } from 'vitest'
import { createMockStore } from './store'
import { STORAGE_KEY } from './persist'

describe('workspace log', () => {
  beforeEach(() => localStorage.clear())

  it('derives a role change from member.role_changed', () => {
    const s = createMockStore({ persist: false })
    const ws = s.workspaces[0].id
    s.appendWs(ws, { type: 'member.role_changed', person: 'p_tom', role: 'member' })
    expect(s.workspaceList()[0].members.find((m) => m.person === 'p_tom')?.role).toBe('member')
  })

  it('persists workspace events and reloads them', () => {
    const a = createMockStore({ persist: true })
    a.appendWs(a.workspaces[0].id, { type: 'addon.disabled', name: 'wiki' })
    const b = createMockStore({ persist: true })
    expect(b.workspaceList()[0].addons.wiki.enabled).toBe(false)
  })

  it('discards v1 storage instead of crashing', () => {
    localStorage.setItem('orch-mock', JSON.stringify({ events: { 'DEMO-0043': [{ junk: true }] } }))
    localStorage.setItem(STORAGE_KEY, '{not json')
    expect(() => createMockStore({ persist: true })).not.toThrow()
    expect(createMockStore({ persist: true }).ticket('DEMO-0043')?.key).toBe('DEMO-0043')
  })

  it('reset() drops workspace events too', () => {
    const s = createMockStore({ persist: false })
    s.appendWs(s.workspaces[0].id, { type: 'workspace.renamed', name: 'X' })
    s.reset()
    expect(s.workspaceList()[0].name).not.toBe('X')
  })
})
```

- [ ] **Step 2: Run** `npx vitest run src/mocks/workspace-log.test.ts` — FAIL.
- [ ] **Step 3: Implement.** `persist.ts` as specified (also `localStorage.removeItem('orch-mock')`, the old key,
  once). `workspace-log.ts` exports `foldWorkspace(seed: Workspace, events: WorkspaceEvent[]): Workspace` (a pure
  reducer; one `case` per `WorkspaceEventType`; unknown types are ignored) and `foldGrants(seed: GrantInfo[], events)`.
  Move the store's `load`/`save` onto `PersistedV2`. Workspace-event actors are `{ kind: 'person', id: viewer }`
  unless given. The seq is per workspace.
- [ ] **Step 4: Run** the new tests and `npx vitest run src/mocks` — PASS.
- [ ] **Step 5: Commit** `mock: versioned persistence and the workspace event log`

### Task 2: Addon module registry and per-addon state

Today all addon actions are one `switch` in `store.ts`, and addon pages can only bind to `workspace`/`ticket`.
Iteration 3 needs ~16 addons with their own changing state.

**Files:**
- Create: `src/mocks/addons/registry.ts`, `src/mocks/addons/{publish,github,usage,terminals,wiki,estimate}.ts`,
  `src/mocks/addons/registry.test.ts`
- Modify: `src/mocks/store.ts` (remove the `switch`, delegate), `src/mocks/router.ts`, `src/api/client.ts`,
  `src/addon-ui/slots.tsx`, `src/addon-ui/AddonNode.tsx` (send `ws`), `src/app/pages/AddonPage.tsx`

**Interfaces:**
- Produces in `src/mocks/addons/registry.ts`:

```ts
export interface AddonCtx {
  store: MockStore
  ws: string                       // workspace id
  viewer: string
  ticket?: string
  body: Record<string, unknown>
  state: Record<string, unknown>   // this addon's state in this workspace (mutable; saved after the action)
}
export interface MockAddon {
  name: string
  /** Initial state per workspace (seed). */
  seed(ws: string, store: MockStore): Record<string, unknown>
  /** Optional derived fields merged into GET .../state (e.g. counts). */
  view?(state: Record<string, unknown>, ctx: Omit<AddonCtx, 'body' | 'state'>): Record<string, unknown>
  actions: Record<string, (ctx: AddonCtx) => AddonActionResult>
}
export function registerAddon(a: MockAddon): void
export function getAddon(name: string): MockAddon | undefined
```

- New mock route: `GET /api/workspaces/:ws/addons/:name/state` → `view(state)` merged over `state`; 404 when the
  addon is not enabled in that workspace.
- `POST /api/addons/:name/actions/:id` body now carries `ws` (required) and optional `ticket`.
- Client: `getAddonState(ws: string, name: string): Promise<Record<string, unknown>>`.
- `SlotContext` gains `addon?: Record<string, unknown>`; bindings read `${addon.shares.length}` etc. `useSlot` and
  `AddonPage` fetch the state with query key `['addon-state', ws, name]`, and every addon action invalidates
  that key plus `['ticket']` and `['today']`.

- [ ] **Step 1: Write failing tests** `src/mocks/addons/registry.test.ts`

```ts
import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'

const setup = () => {
  const store = createMockStore({ persist: false })
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws: store.workspaces[0].id }
}

describe('addon registry', () => {
  it('serves per-workspace addon state', async () => {
    const { api, ws } = setup()
    const s = await api.getAddonState(ws, 'publish')
    expect(Array.isArray(s.apps)).toBe(true)
  })
  it('an action mutates state and the next read sees it', async () => {
    const { api, ws } = setup()
    await api.runAddonAction('publish', 'share', { ws, ticket: 'DEMO-0043' })
    const s = await api.getAddonState(ws, 'publish')
    expect((s.shares as unknown[]).length).toBeGreaterThan(0)
  })
  it('404s for an addon disabled in the workspace', async () => {
    const { api, store, ws } = setup()
    store.appendWs(ws, { type: 'addon.disabled', name: 'wiki' })
    await expect(api.getAddonState(ws, 'wiki')).rejects.toMatchObject({ status: 404 })
  })
  it('keeps the six iteration-1 actions working', async () => {
    const { api, ws } = setup()
    for (const [a, id] of [['publish', 'decide'], ['estimate', 'save_settings'], ['github', 'refresh'], ['terminals', 'save_settings'], ['usage', 'save_settings'], ['wiki', 'open']] as const)
      expect((await api.runAddonAction(a, id, { ws })).ok).toBe(true)
  })
})
```

- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Move each `case` of `store.addonAction` into its addon module unchanged in behaviour.
  The store keeps `addonState(ws, name)` (lazily seeded, persisted in `PersistedV2.addonState`) and
  `runAddon(name, id, body)`. Register the modules in `src/mocks/addons/index.ts`, imported by `store.ts`.
- [ ] **Step 4: Run** `npx vitest run src/mocks src/addon-ui src/app/shell` — PASS (existing addon tests unchanged).
- [ ] **Step 5: Commit** `mock: addon modules with per-workspace state`

### Task 3: New declarative node types **[security review]**

**Files:**
- Modify: `src/addon-ui/nodes.ts`, `src/addon-ui/AddonNode.tsx`, `src/addon-ui/AddonNode.test.tsx`
- Create: `src/addon-ui/FrameNode.tsx`

**Interfaces (all additive; the set stays closed):**

```ts
const itemAction = z.object({ label: z.string().max(40), action: actionId, args: z.record(z.string(), scalar).optional(),
  variant: z.enum(['primary', 'secondary', 'ghost', 'danger']).default('ghost') })
// listNode items gain: actions: z.array(itemAction).max(3).optional(), status: z.enum(['ok','warn','error','idle','running']).optional()
// tableNode gains: rowActions: z.array(itemAction).max(3).optional()   (args may reference row fields as "$row.<key>")
export const alertNode = z.object({ type: z.literal('alert'), tone: z.enum(['info', 'success', 'warn', 'error']), title: text, text: text.optional() })
export const progressNode = z.object({ type: z.literal('progress'), label: text, value: z.number().min(0), max: z.number().positive() })
export const frameNode = z.object({ type: z.literal('frame'), title: z.string().max(120), html: z.string().max(200_000), height: z.number().int().min(80).max(1200).default(320) })
export const terminalNode = z.object({ type: z.literal('terminal'), session: z.string().regex(/^[a-z0-9_-]{1,40}$/) })
```

- Item/row actions post `{ ws, ticket?, ...args }` to the addon action.
- `frame`: `<iframe sandbox="allow-scripts" srcDoc=... referrerPolicy="no-referrer">` — **never** `allow-same-origin`,
  `allow-top-navigation`, `allow-popups` or `allow-forms`. A CSP meta tag `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:`
  is prepended to the document. Rendered inside `AddonFrame` with the title.
- `terminal`: renders core's `<TerminalView session=…/>` (built in Task 21); until then it renders an `alert`
  "Terminal sessions arrive with the terminals addon". Only addons whose manifest has the `pty` capability **and a
  current grant** may use it; otherwise the node shows the "could not be shown" box.

- [ ] **Step 1: Write failing tests** (append to `AddonNode.test.tsx`)

```tsx
it('renders list item actions and posts the args', async () => {
  const post = vi.spyOn(api, 'runAddonAction').mockResolvedValue({ ok: true, message: 'done' })
  renderNode({ type: 'list', items: [{ title: 'share/a', actions: [{ label: 'Revoke', action: 'revoke', args: { id: 'a' }, variant: 'danger' }] }] }, { addon: 'publish' })
  await userEvent.click(screen.getByRole('button', { name: 'Revoke' }))
  expect(post).toHaveBeenCalledWith('publish', 'revoke', expect.objectContaining({ id: 'a' }))
})
it('renders a frame sandboxed without same-origin', () => {
  renderNode({ type: 'frame', title: 'Bars', html: '<p>hi</p>' }, { addon: 'widgets' })
  const f = screen.getByTitle('Bars') as HTMLIFrameElement
  expect(f.getAttribute('sandbox')).toBe('allow-scripts')
  expect(f.srcdoc).toContain("default-src 'none'")
})
it('refuses a terminal node from an addon without a pty grant', () => {
  renderNode({ type: 'terminal', session: 't1' }, { addon: 'wiki' })
  expect(screen.getByText(/could not be shown/i)).toBeInTheDocument()
})
it('renders alert and progress', () => {
  renderNode({ type: 'stack', children: [{ type: 'alert', tone: 'warn', title: 'Budget at 80%' }, { type: 'progress', label: 'Children', value: 7, max: 25 }] }, { addon: 'usage' })
  expect(screen.getByRole('status')).toHaveTextContent('Budget at 80%')
  expect(screen.getByRole('progressbar', { name: 'Children' })).toHaveAttribute('aria-valuenow', '7')
})
```

(`renderNode(node, { addon })` is the helper already in the file; extend it to accept the addon name if needed.)

- [ ] **Step 2: Run** `npx vitest run src/addon-ui` — FAIL.
- [ ] **Step 3: Implement** the schemas and renderers. `warn` uses the existing warning token, **not orange**.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `addon-ui: item actions, alert, progress, sandboxed frame, terminal node`

### Task 4: Live updates and the simulator

Iteration 3's "start agent", schedules and the AI Factory must look alive: events arrive while the owner watches.

**Files:**
- Create: `src/mocks/sim.ts`, `src/mocks/sim.test.ts`, `src/app/live.ts`
- Modify: `src/mocks/router.ts` (cursor route), `src/api/client.ts`, `src/app/shell/Shell.tsx` (mount `useLiveUpdates`)

**Interfaces:**
- `GET /api/workspaces/:ws/cursor` → `{ cursor: number }`, a counter that increases on every ticket or workspace
  append in that workspace. Client: `getCursor(ws)`.
- `useLiveUpdates()`: polls `getCursor` every 2 s (only while `document.visibilityState === 'visible'`); when the
  cursor moves it invalidates `['today']`, `['tickets']`, `['ticket']`, `['agents']`, `['addon-state']`, `['workspaces']`.
- `sim.ts`:

```ts
export interface SimStep { afterMs: number; run: (store: MockStore) => void }
export class Simulator {
  constructor(store: MockStore, clock?: { setTimeout: typeof setTimeout; clearTimeout: typeof clearTimeout })
  play(id: string, steps: SimStep[]): void   // replaces a script with the same id
  stop(id: string): void
  running(): string[]
}
```

The store owns one `Simulator`; `reset()` stops all scripts.

- [ ] **Step 1: Write failing test** `src/mocks/sim.test.ts`

```ts
import { describe, expect, it, vi } from 'vitest'
import { createMockStore } from './store'

describe('simulator', () => {
  it('plays steps in order on the clock and moves the cursor', () => {
    vi.useFakeTimers()
    const s = createMockStore({ persist: false })
    const ws = s.workspaceOf('DEMO-0043')!.id
    const before = s.cursor(ws)
    s.sim.play('demo', [
      { afterMs: 1000, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'step 1' }) },
      { afterMs: 1000, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'step 2' }) },
    ])
    vi.advanceTimersByTime(1000)
    expect(s.cursor(ws)).toBe(before + 1)
    vi.advanceTimersByTime(1000)
    expect(s.eventsOf('DEMO-0043').at(-1)?.text).toBe('step 2')
    vi.useRealTimers()
  })
  it('reset stops running scripts', () => {
    vi.useFakeTimers()
    const s = createMockStore({ persist: false })
    s.sim.play('x', [{ afterMs: 500, run: (st) => st.append('DEMO-0043', { type: 'log.added', text: 'late' }) }])
    s.reset()
    vi.advanceTimersByTime(1000)
    expect(s.eventsOf('DEMO-0043').some((e) => e.text === 'late')).toBe(false)
    vi.useRealTimers()
  })
})
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `npx vitest run src/mocks` — PASS.
- [ ] **Step 5: Commit** `mock: workspace cursor, live updates and a simulator for scripted runs`

**Phase 0 gate:** full suite ×3, `npm run typecheck`, `npm run build`. Push.

---

# Phase 1: Iteration 2 (core pages)

### Task 5: Tickets list and search

**Files:**
- Create: `src/app/pages/tickets/{index.tsx,TicketsTable.tsx,Filters.tsx,tickets.test.tsx}`
- Modify: `src/app/router.tsx` (replace the placeholder), `src/mocks/router.ts`, `src/api/client.ts`

**Interfaces:**
- `ListTicketsParams` gains `priority?: Priority[]`, `label?: string`, `person?: string` (owner, assignee or claim
  `for`), `needs?: 'me' | 'agent' | 'nobody'` (turn), `sort?: 'updated' | 'priority' | 'key' | 'status'`,
  `restricted?: boolean`. The mock implements each. `q` also searches **body sections** and returns
  `match?: { section: keyof BodySections; snippet: string }` on `TicketSummary` (snippet ≤ 140 chars, the hit wrapped
  in `«»`).
- The page keeps filters in the router search params, so Back works and a saved view (Task 6) is just params.

**UI:**
- Table columns: Key (mono), Title (with label chips and the search snippet under it), Type, Priority, Status,
  People (avatars), Turn ("you", agent, nobody), Progress (tasks and ACs), Updated (relative). Addon columns from
  `board.card_field` contributions (estimate points) with the A badge in the header.
- Filter bar: search box (`/` focuses it, debounce 200 ms), status, type, priority, people, "needs: me / agent",
  label. "Clear filters". Counts per status as toggle chips.
- Keyboard: `j`/`k` move the row focus, `Enter` opens the ticket, `x` selects; bulk bar with "Set status" and "Add
  label" (owner and maintainer only).
- Empty state ("No tickets match. Clear filters") and a CLI hint (`orch list --status open`).

- [ ] **Step 1: Write failing tests** `src/app/pages/tickets/tickets.test.tsx`

```tsx
import { describe, expect, it } from 'vitest'
import { screen, within } from '@testing-library/react'
import { renderApp } from '@/test/renderApp'

describe('Tickets page', () => {
  it('lists the workspace tickets in a table', async () => {
    renderApp('/tickets')
    const table = await screen.findByRole('table', { name: 'Tickets' })
    expect(within(table).getByText('DEMO-0043')).toBeInTheDocument()
  })
  it('full-text search finds a ticket by its body and shows a snippet', async () => {
    const { user } = renderApp('/tickets')
    await user.type(await screen.findByRole('searchbox', { name: 'Search tickets' }), 'fct_billing')
    expect(await screen.findByText(/«fct_billing»/)).toBeInTheDocument()
  })
  it('filters by status and clears', async () => {
    const { user } = renderApp('/tickets')
    await user.click(await screen.findByRole('button', { name: /^testing/i }))
    const rows = within(screen.getByRole('table', { name: 'Tickets' })).getAllByRole('row').slice(1)
    rows.forEach((r) => expect(r).toHaveTextContent(/testing/i))
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
  })
  it('shows an empty state', async () => {
    const { user } = renderApp('/tickets')
    await user.type(await screen.findByRole('searchbox', { name: 'Search tickets' }), 'zzzz-no-match')
    expect(await screen.findByText(/No tickets match/)).toBeInTheDocument()
  })
  it('opens a ticket with Enter on the focused row', async () => {
    const { user } = renderApp('/tickets')
    await screen.findByRole('table', { name: 'Tickets' })
    await user.keyboard('j{Enter}')
    expect(await screen.findByRole('heading', { level: 1 })).toBeInTheDocument()
  })
  it('hides bulk actions for the viewer', async () => {
    const { user } = renderApp('/tickets', { viewer: 'p_tom' })
    await screen.findByRole('table', { name: 'Tickets' })
    await user.keyboard('jx')
    expect(screen.queryByRole('button', { name: 'Set status' })).toBeNull()
  })
})
```

Add a mock test in `src/mocks/router.test.ts`: `q=fct_billing` returns DEMO-0043 with `match.section` set; `sort=priority` puts `urgent` first.

- [ ] **Step 2: Run** `npx vitest run src/app/pages/tickets src/mocks/router.test.ts` — FAIL.
- [ ] **Step 3: Implement** with shadcn `Table`. Reuse `TicketCard` helpers from `board/lib.tsx` for chips (move them
  to `src/app/pages/shared/` if both pages need them).
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `tickets: list, filters, full-text search, keyboard navigation`

### Task 6: Saved views

**Files:**
- Create: `src/app/pages/tickets/SavedViews.tsx`; test in `tickets.test.tsx`
- Modify: `src/mocks/router.ts`, `src/api/client.ts`, `src/api/types.ts`, fixtures (`views.json`, two seeded views)

**Interfaces:**

```ts
export interface SavedView { id: string; name: string; owner: string; shared: boolean; params: ListTicketsParams }
// GET  /api/workspaces/:ws/views            -> SavedView[] (own + shared)
// POST /api/workspaces/:ws/views            { name, shared, params } -> SavedView   (view.saved)
// POST /api/workspaces/:ws/views/:id/delete -> { ok: true }                         (view.deleted; owner of the view only)
```

Seeded: "My open work" (Severin, personal: `needs=me`), "Release blockers" (shared: `priority=urgent,high`,
`status=open,in-progress,waiting`). Viewers may save personal views but not shared ones.

**UI:** a views strip above the filters: "All tickets", the views, and "Save view…" (dialog: name, "Share with the
workspace"). Active view highlighted; editing filters shows "Modified · Save · Revert".

- [ ] **Step 1: Failing tests**

```tsx
it('applies a seeded shared view', async () => {
  const { user } = renderApp('/tickets')
  await user.click(await screen.findByRole('tab', { name: 'Release blockers' }))
  const rows = within(screen.getByRole('table', { name: 'Tickets' })).getAllByRole('row').slice(1)
  rows.forEach((r) => expect(r).toHaveTextContent(/urgent|high/i))
})
it('saves the current filters as a view', async () => {
  const { user } = renderApp('/tickets')
  await user.click(await screen.findByRole('button', { name: /^bug/i }))
  await user.click(screen.getByRole('button', { name: 'Save view…' }))
  await user.type(screen.getByLabelText('Name'), 'Bugs')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  expect(await screen.findByRole('tab', { name: 'Bugs' })).toHaveAttribute('aria-selected', 'true')
})
it('the viewer cannot share a view', async () => {
  const { user } = renderApp('/tickets', { viewer: 'p_tom' })
  await user.click(await screen.findByRole('button', { name: 'Save view…' }))
  expect(screen.getByLabelText('Share with the workspace')).toBeDisabled()
})
```

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `tickets: saved views, personal and shared`

### Task 7: New ticket page

**Files:**
- Create: `src/mocks/sections.ts`, `src/mocks/tickets-create.ts`, `src/mocks/tickets-create.test.ts`,
  `src/app/pages/new-ticket/{index.tsx,SectionsEditor.tsx,PeoplePicker.tsx,new-ticket.test.tsx}`
- Modify: `src/app/router.tsx` (`/tickets/new`), `src/app/shell/NewTicketDialog.tsx` (delete; the shell button and
  `c` shortcut navigate to `/tickets/new`), `src/mocks/router.ts`, `src/api/client.ts`, `src/api/types.ts`

**Interfaces:**

```ts
// src/mocks/sections.ts (imported by the UI through src/api/types.ts re-export, since it is pure data)
export type SectionNeed = 'required' | 'optional' | 'absent'
export const SECTIONS_BY_TYPE: Record<TicketType, Record<keyof BodySections, SectionNeed>>
// From spec §4: 'yes' -> 'required', 'optional' -> 'optional', '—' -> 'absent'.
// spike: requirements = "questions to answer", verification = "findings" (labels in SECTION_LABEL_BY_TYPE).

export interface NewTicketRequest {
  type: TicketType; title: string; priority: Priority; size: Size | null; labels: string[]
  parent: string | null; due: string | null; visibility: Visibility
  people: { owner: string | null; assignees: string[]; reviewers: string[] }
  sections: BodySections
  acceptance: string[]           // texts; ids AC1.. assigned by the host
}
// POST /api/workspaces/:ws/tickets  -> 201 { ok: true, ticket: TicketDocument }
// errors: 400 validation { code: 'validation.section_missing' | 'validation.title' | 'validation.parent' | 'validation.size' }
```

**Rules (record in DECISIONS-LOG):** at creation only **Title** and **Requirements** are required (epic: also
Summary). Other sections the type marks "yes" are shown with "needed before the plan gate" and may stay empty.
Sections marked `absent` are not shown. Title 3–120 chars. `parent` must be an epic in the same workspace. A new
ticket is `backlog`; the next key is `max(key) + 1`, zero-padded to 4. Viewers cannot create (403).

**UI:** one page. Type segmented control (feature, bug, chore, spike, epic) at the top; switching type keeps
the text typed into sections that both types share. Title (large input), then the type's sections as markdown
textareas with a Preview toggle (`SafeMarkdown`), acceptance criteria list (add/remove), and a right column: priority,
size, labels (combobox with existing labels), parent (epic picker), due, people (owner, assignees, reviewers), visibility
("Workspace" or "Restricted to…"). Addon fields from `settings`-declared ticket fields are out of scope; the estimate
addon contributes a "Points" field via its `ticket.panel` form after creation. Footer: "Create" (`⌘↵`), "Create and
open another", Cancel. Leaving with unsaved text asks "Discard this draft?". The draft autosaves to
`localStorage` (per viewer, try/catch) and restores with a "Draft restored · Discard" bar.

- [ ] **Step 1: Failing tests** `src/mocks/tickets-create.test.ts`

```ts
it('creates a backlog ticket with the next key', async () => {
  const { api, ws } = setup()
  const r = await api.createTicket(ws, { ...base, type: 'bug', title: 'Login loops', sections: { requirements: 'Stop the loop' } })
  expect(r.ticket.status).toBe('backlog')
  expect(r.ticket.key).toMatch(/^DEMO-\d{4}$/)
  expect(r.ticket.body.requirements).toBe('Stop the loop')
})
it('refuses a missing requirements section', async () => {
  const { api, ws } = setup()
  await expect(api.createTicket(ws, { ...base, sections: {} })).rejects.toMatchObject({ code: 'validation.section_missing' })
})
it('requires a summary for an epic', async () => {
  const { api, ws } = setup()
  await expect(api.createTicket(ws, { ...base, type: 'epic', sections: { requirements: 'x' } })).rejects.toMatchObject({ code: 'validation.section_missing' })
})
it('refuses a parent that is not an epic', async () => {
  const { api, ws } = setup()
  await expect(api.createTicket(ws, { ...base, parent: 'DEMO-0043', sections: { requirements: 'x' } })).rejects.toMatchObject({ code: 'validation.parent' })
})
it('the viewer cannot create', async () => {
  const { api, store, ws } = setup(); store.setViewer('p_tom')
  await expect(api.createTicket(ws, { ...base, sections: { requirements: 'x' } })).rejects.toMatchObject({ status: 403 })
})
it('a created ticket survives a reload', async () => {
  const a = createMockStore({ persist: true }); const api = apiOf(a)
  const { ticket } = await api.createTicket(a.workspaces[0].id, { ...base, sections: { requirements: 'x' } })
  expect(createMockStore({ persist: true }).ticket(ticket.key)?.title).toBe(base.title)
})
```

(`base` = a valid feature request with title "A new ticket"; `setup`/`apiOf` as in Task 2.)

UI tests `new-ticket.test.tsx`:

```tsx
it('shows the sections a bug needs and hides absent ones', async () => {
  const { user } = renderApp('/tickets/new')
  await user.click(await screen.findByRole('radio', { name: 'chore' }))
  expect(screen.queryByLabelText('Out of scope')).toBeNull()
  await user.click(screen.getByRole('radio', { name: 'bug' }))
  expect(screen.getByLabelText('Out of scope')).toBeInTheDocument()
})
it('keeps shared section text when switching type', async () => {
  const { user } = renderApp('/tickets/new')
  await user.type(await screen.findByLabelText(/^Requirements/), 'Must not loop')
  await user.click(screen.getByRole('radio', { name: 'bug' }))
  expect(screen.getByLabelText(/^Requirements/)).toHaveValue('Must not loop')
})
it('creates the ticket and opens it', async () => {
  const { user } = renderApp('/tickets/new')
  await user.type(await screen.findByLabelText('Title'), 'Export fails on empty month')
  await user.type(screen.getByLabelText(/^Requirements/), 'Empty months export a header row')
  await user.click(screen.getByRole('button', { name: 'Create' }))
  expect(await screen.findByRole('heading', { level: 1, name: /Export fails on empty month/ })).toBeInTheDocument()
  expect(screen.getByText('backlog')).toBeInTheDocument()
})
it('blocks Create and says why when requirements are empty', async () => {
  const { user } = renderApp('/tickets/new')
  await user.type(await screen.findByLabelText('Title'), 'No reqs')
  await user.click(screen.getByRole('button', { name: 'Create' }))
  expect(await screen.findByText(/Requirements are needed/)).toBeInTheDocument()
})
it('the viewer sees why they cannot create', async () => {
  renderApp('/tickets/new', { viewer: 'p_tom' })
  expect(await screen.findByText(/Viewers cannot create tickets/)).toBeInTheDocument()
})
```

- [ ] **Step 2–4:** run (FAIL), implement, run `npx vitest run src/mocks/tickets-create.test.ts src/app/pages/new-ticket src/app/shell` (PASS).
- [ ] **Step 5: Commit** `new ticket: type-aware sections, people, parent, draft autosave`

### Task 8: Full ⌘K palette

**Files:**
- Modify: `src/app/shell/CommandPalette.tsx` (split into `src/app/shell/palette/{index.tsx,groups.tsx,recent.ts}` if it
  grows past ~300 lines), `src/app/shell/shell.test.tsx`

**Behaviour:**
- Groups, in this order: **Recent** (last 8 visited tickets and pages, per viewer, localStorage with try/catch),
  **On this ticket** (only on a ticket page: Claim / Release, Comment, Ask a question, Move to…, Approve <gate> and
  Give verdict, which open core's `SignDialog`; each hidden when the action is not allowed for the viewer),
  **Tickets** (server search via `listTickets({ q })`, debounced 150 ms, top 8, key + title + status), **Go to**
  (Today, Board, Tickets, Agents, Settings, each addon page with the A badge), **Create** (New ticket, Save view…),
  **Addon commands** (every enabled addon's `commands`, with the A badge, run through `runAddonAction` with `ws`
  and the current ticket), **Workspace** (switch to INT / CLI).
- Typing `>` restricts to commands, `#` to tickets, `@` to people (opens Tickets filtered by that person).
- Every item shows its shortcut when it has one (`g t` Today, `g b` Board, `g l` Tickets, `g a` Agents, `c` New ticket,
  `[` sidebar). Add the `g x` two-key shortcuts in the shell.

- [ ] **Step 1: Failing tests** (append to `shell.test.tsx`)

```tsx
it('finds a ticket by title and opens it', async () => {
  const { user } = renderApp('/')
  await screen.findByRole('heading', { name: 'Today' })
  await user.keyboard('{Control>}k{/Control}')
  await user.type(screen.getByPlaceholderText(/Search tickets/), 'billing')
  await user.click(await screen.findByRole('option', { name: /DEMO-0043/ }))
  expect(await screen.findByRole('heading', { level: 1, name: /billing/i })).toBeInTheDocument()
})
it('offers ticket actions only on a ticket page, and opens the sign dialog for approve', async () => {
  const { user } = renderApp('/ticket/DEMO-0043')
  await screen.findByRole('heading', { level: 1 })
  await user.keyboard('{Control>}k{/Control}')
  expect(await screen.findByRole('group', { name: 'On this ticket' })).toBeInTheDocument()
})
it('> limits the list to commands', async () => {
  const { user } = renderApp('/')
  await screen.findByRole('heading', { name: 'Today' })
  await user.keyboard('{Control>}k{/Control}')
  await user.type(screen.getByPlaceholderText(/Search tickets/), '>')
  expect(screen.queryByRole('group', { name: 'Tickets' })).toBeNull()
  expect(screen.getByRole('group', { name: 'Addon commands' })).toBeInTheDocument()
})
it('remembers recently visited tickets', async () => {
  const { user } = renderApp('/ticket/DEMO-0043')
  await screen.findByRole('heading', { level: 1 })
  await user.keyboard('{Control>}k{/Control}')
  expect(within(await screen.findByRole('group', { name: 'Recent' })).getByText(/DEMO-0043/)).toBeInTheDocument()
})
it('g then b goes to the board', async () => {
  const { user } = renderApp('/')
  await screen.findByRole('heading', { name: 'Today' })
  await user.keyboard('gb')
  expect(await screen.findByRole('heading', { name: 'Board' })).toBeInTheDocument()
})
```

- [ ] **Step 2–4:** run (FAIL), implement, run `npx vitest run src/app/shell` (PASS).
- [ ] **Step 5: Commit** `palette: tickets, ticket actions, navigation, addon commands, recent items`

### Task 9: Agents page **[security review]**

**Files:**
- Create: `src/app/pages/agents/{index.tsx,Sessions.tsx,Grants.tsx,AgentActivity.tsx,agents.test.tsx}`
- Modify: `src/api/types.ts`, `src/api/client.ts`, `src/mocks/router.ts`, `src/mocks/store.ts`,
  fixtures (`me.json` agents, `grants.json`, refusal events in `demo.json`), `src/app/router.tsx`

**Interfaces:**

```ts
export interface AgentSession extends AgentInfo {
  parent: string | null                   // subagents: "s_77c2.2" has parent "s_77c2"
  harness: 'claude-code' | 'codex' | 'ci'
  model?: string
  state: 'working' | 'waiting' | 'idle' | 'stopped'
  waiting_on?: { kind: 'question' | 'approval' | 'verdict'; ticket: string; ref?: string }
}
export interface AgentActivityItem {
  at: string; ticket: string; session: string; agent: string; for: string
  type: string               // task.started, task.done, ask, claim.taken, refused, ...
  summary: string
  refusal?: { code: string; message: string; retryable: boolean; stop: boolean } // stop: the third same refusal
}
// GET  /api/workspaces/:ws/agents          -> AgentSession[]   (extends today's AgentInfo; existing callers keep working)
// GET  /api/workspaces/:ws/agents/activity -> AgentActivityItem[] (newest first, 50)
// GET  /api/workspaces/:ws/grants          -> GrantInfo[]
// POST /api/workspaces/:ws/grants          { hours: 1..12, scope: 'all' } -> GrantInfo   (grant.issued; person = viewer; human only)
// POST /api/workspaces/:ws/grants/:id/revoke -> GrantInfo                                  (grant.revoked; the grant's person or the owner)
```

Seed: Severin's grant `gr_01J9Z8` until 18:00 (2 sessions + 2 subagents), Mara's grant until 17:00 (1 session), a
CI grant (scope `ci`, expired 09:00), one revoked grant from yesterday. Activity: ~25 items over DEMO tickets,
including 3 refusals (`human_only approve`, `claim.held`, a repeated `lease.held` that hit the stop rule).

**Rules:** issuing and revoking are human-only and go through `SignDialog` (Touch ID simulation). Revoking ends the
claims and leases of the sessions using that grant (`claim.released` with `reason: 'grant revoked'` on each ticket),
and their state becomes `stopped`. A viewer sees the page read-only.

**UI:**
- Header summary: "3 sessions working · 1 waiting on you · grant until 18:00". "Issue grant…" (hours slider 1–12, default 8).
- Sessions: a tree per person (session → subagents) with ticket claim, task leases (ticket/T3), model, state,
  last seen, and "waiting on you" linking to the Today item.
- Grants: table (person, scope, issued, until with a countdown, sessions, state) with "Revoke" (danger) per row.
- Agent activity: a feed with filters (person, ticket, refusals only). Refusals show the code in mono, the
  message and a red "stopped" chip when `stop`.

- [ ] **Step 1: Failing tests** `agents.test.tsx`

```tsx
it('shows sessions with their subagents and leases', async () => {
  renderApp('/agents')
  const tree = await screen.findByRole('tree', { name: 'Sessions' })
  expect(within(tree).getByText(/s_77c2\.1/)).toBeInTheDocument()
  expect(within(tree).getByText(/DEMO-0043\/T3/)).toBeInTheDocument()
})
it('revokes a grant after signing, and its sessions stop', async () => {
  const { user } = renderApp('/agents')
  const row = await screen.findByRole('row', { name: /gr_01J9Z8/ })
  await user.click(within(row).getByRole('button', { name: 'Revoke' }))
  await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
  expect(await within(screen.getByRole('row', { name: /gr_01J9Z8/ })).findByText('revoked')).toBeInTheDocument()
  expect(within(screen.getByRole('tree', { name: 'Sessions' })).getAllByText('stopped').length).toBeGreaterThan(0)
})
it('lists refusals and marks the stop rule', async () => {
  const { user } = renderApp('/agents')
  await user.click(await screen.findByRole('checkbox', { name: 'Refusals only' }))
  expect(await screen.findByText('human_only')).toBeInTheDocument()
  expect(screen.getByText('stopped after 3 refusals')).toBeInTheDocument()
})
it('the viewer cannot revoke or issue', async () => {
  renderApp('/agents', { viewer: 'p_tom' })
  await screen.findByRole('tree', { name: 'Sessions' })
  expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Issue grant…' })).toBeNull()
})
```

Mock tests in `router.test.ts`: revoking releases the claim on DEMO-0043; Mara cannot revoke Severin's grant (403);
an agent actor cannot issue a grant (`human_only`).

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS). Opus review of the revoke and issue paths.
- [ ] **Step 5: Commit** `agents: sessions, leases, grants with signed revoke, activity and refusals`

### Task 10: Settings shell, general, members and gate policies **[security review]**

**Files:**
- Create: `src/app/pages/settings/{index.tsx,General.tsx,Members.tsx,Gates.tsx,DangerZone.tsx,settings.test.tsx}`
- Modify: `src/mocks/router.ts`, `src/api/client.ts`, `src/api/types.ts`, `src/app/router.tsx`
  (`/settings/$tab`, `/settings` → `general`)

**Interfaces:**

```ts
// POST /api/workspaces/:ws/settings  body is one of:
//  { op: 'rename', name }                                     owner
//  { op: 'member.add', person, name, role }                   owner (role 'owner' not assignable here)
//  { op: 'member.role', person, role }                        owner; the last owner cannot be demoted
//  { op: 'member.remove', person }                            owner; not yourself
//  { op: 'gate.policy', gate, approvers, count, not? }        owner; count 1..3
// -> { ok: true, workspace: Workspace } ; every op except rename is signed in the UI (SignDialog)
export interface WorkspaceIdentity { uuid: string; prefix: string; created_at: string; key_fingerprint: string; epoch: number }
// GET /api/workspaces/:ws/identity -> WorkspaceIdentity
```

**UI:** settings page with a left sub-nav: General, Members & roles, Gate policies, Addons (Task 11), and one entry per
enabled addon with a `settings` contribution (Task 12, each with the A badge).
- General: name (editable), prefix and UUID (read-only, copy buttons), key fingerprint and epoch, **Relay**
  (placeholder card: "Not connected · arrives with orch-relay (P3)" and a disabled "Connect" button), Danger zone
  (Export workspace (mock download of a JSON), Archive workspace: type the prefix to confirm, then the mock refuses
  with "Archiving is CLI-only: `orch workspace archive`").
- Members & roles: table (person, role select, devices count, last seen); "Add member" (name, person id, role); remove.
  A role explainer popover lists what each role can do.
- Gate policies: per gate (requirements, plan, verify): approvers (role group or people), how many, "not the
  assignees" toggle, and a live sentence: "Plan needs 1 approval from owners or maintainers, not the assignees."
  Changing a policy warns: "Open approvals stay valid; new approvals use the new policy."
- Non-owners see everything read-only with "Only owners change settings."

- [ ] **Step 1: Failing tests** `settings.test.tsx`

```tsx
it('changes a member role after signing', async () => {
  const { user } = renderApp('/settings/members')
  const row = await screen.findByRole('row', { name: /Tom/ })
  await user.selectOptions(within(row).getByRole('combobox', { name: 'Role' }), 'member') // if Radix Select: click + option
  await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
  expect(await within(screen.getByRole('row', { name: /Tom/ })).findByText(/member/)).toBeInTheDocument()
})
it('refuses to demote the last owner', async () => {
  const { user } = renderApp('/settings/members')
  const row = await screen.findByRole('row', { name: /Severin/ })
  expect(within(row).getByRole('combobox', { name: 'Role' })).toBeDisabled()
  await user.hover(within(row).getByRole('combobox', { name: 'Role' }))
  expect(await screen.findByText(/last owner/)).toBeInTheDocument()
})
it('describes a gate policy in a sentence and saves it', async () => {
  const { user } = renderApp('/settings/gates')
  expect(await screen.findByText(/Plan needs 1 approval from/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Plan: 2 approvals' }))
  await user.click(await screen.findByRole('button', { name: /Sign with Touch ID/ }))
  expect(await screen.findByText(/Plan needs 2 approvals/)).toBeInTheDocument()
})
it('Mara sees settings read-only', async () => {
  renderApp('/settings/members', { viewer: 'p_mara' })
  expect(await screen.findByText('Only owners change settings.')).toBeInTheDocument()
})
it('shows the relay as not connected', async () => {
  renderApp('/settings/general')
  expect(await screen.findByText(/Not connected/)).toBeInTheDocument()
})
```

(Use whatever control the page actually renders; keep the accessible names above stable.)

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `settings: general, identity, members and roles, gate policies, danger zone`

### Task 11: Addon manager with signed capability grants **[security review]**

**Files:**
- Create: `src/app/pages/settings/addons/{index.tsx,AddonRow.tsx,GrantDialog.tsx,Catalog.tsx,addons.test.tsx}`,
  `src/mocks/fixtures/catalog.json`
- Modify: `src/api/types.ts`, `src/mocks/router.ts`, `src/mocks/workspace-log.ts`, `src/api/client.ts`

**Interfaces:**

```ts
export interface AddonGrant { version: string; capabilities: string[]; package_sha256: string; at: string; by: string }
// AddonManifest gains:
//   installed: boolean; package_sha256: string
//   granted: AddonGrant | null                    // the grant for the installed version, null when missing
//   update: { version: string; capabilities: string[]; package_sha256: string; changelog: string } | null
//   status: 'active' | 'disabled' | 'needs_grant' // derived: needs_grant when granted?.version !== version
// GET  /api/workspaces/:ws/addons/catalog   -> AddonManifest[] (installable, not installed)
// POST /api/workspaces/:ws/addons/:name     { op: 'install' | 'grant' | 'enable' | 'disable' | 'update' | 'uninstall' }
//   install   -> addon.installed (status needs_grant, enabled false)
//   grant     -> addon.granted {name, version, package_sha256, capabilities}  (signed; owner only)
//   enable    -> refused 409 'addon.needs_grant' while status is needs_grant
//   update    -> addon.updated; status becomes needs_grant again; the addon is inactive until re-granted
//   uninstall -> addon.uninstalled; its ticket data stays (shown inactive)
```

`GET /api/addons` keeps returning the global manifests; per-workspace status comes from `workspace.addons` (now
`Record<string, { enabled: boolean; status: AddonStatus }>`). Seed: the six iteration-1 addons installed and granted;
`github` has an update 0.6.0 that adds the capability `spawn_agent`; the catalog lists the iteration-3 addons not
installed yet.

**UI:** Addons tab:
- Installed list: badge, title, version, capability chips (each with an explainer tooltip: `network` "talks to
  the internet", `serve_http` "serves pages on your machine", `pty` "opens terminals; never for agents",
  `spawn_agent` "starts agent sessions", `launch` "changes how agents start"), status, enable switch, "Settings",
  "Update to 0.6.0" (shows the **capability diff**: "+ spawn_agent"), uninstall.
- "Browse addons" catalog sheet: first-party chip, description, capabilities, Install.
- GrantDialog (core): lists name, version, sha256 (short, copyable) and capabilities, "Grant and sign". Disabling
  is not signed; granting and updating are.
- An addon with `needs_grant` shows a warning-tone (not orange) callout: "Updated to 0.6.0: grant again to turn it back on."

- [ ] **Step 1: Failing tests** `addons.test.tsx`

```tsx
it('installs from the catalog, requires a signed grant, then enables', async () => {
  const { user } = renderApp('/settings/addons')
  await user.click(await screen.findByRole('button', { name: 'Browse addons' }))
  await user.click(within(await screen.findByRole('article', { name: /Quick tasks/ })).getByRole('button', { name: 'Install' }))
  const row = await screen.findByRole('row', { name: /Quick tasks/ })
  expect(within(row).getByRole('switch', { name: /Enable/ })).toBeDisabled()
  await user.click(within(row).getByRole('button', { name: 'Grant…' }))
  await user.click(await screen.findByRole('button', { name: /Grant and sign/ }))
  await user.click(within(screen.getByRole('row', { name: /Quick tasks/ })).getByRole('switch', { name: /Enable/ }))
  expect(await screen.findByRole('link', { name: /Quick tasks/ })).toBeInTheDocument() // sidebar nav appeared
})
it('an update with a new capability shows the diff and needs a re-grant', async () => {
  const { user } = renderApp('/settings/addons')
  const row = await screen.findByRole('row', { name: /GitHub/ })
  await user.click(within(row).getByRole('button', { name: /Update to 0\.6\.0/ }))
  expect(await screen.findByText('+ spawn_agent')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Update' }))
  expect(await screen.findByText(/grant again to turn it back on/)).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /Code reviews/ })).toBeNull()
})
it('disabling an addon removes its nav, palette commands and Today card', async () => {
  const { user } = renderApp('/settings/addons')
  await user.click(within(await screen.findByRole('row', { name: /Publish/ })).getByRole('switch', { name: /Enable/ }))
  expect(screen.queryByRole('link', { name: /Apps & shares/ })).toBeNull()
})
it('only the owner grants', async () => {
  renderApp('/settings/addons', { viewer: 'p_mara' })
  expect(await screen.findByText('Only owners change settings.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Grant…/ })).toBeNull()
})
```

Mock tests: `enable` before `grant` → 409 `addon.needs_grant`; a grant for the wrong version is refused; the grant
event carries `package_sha256` and the capabilities.

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS). Opus review.
- [ ] **Step 5: Commit** `settings: addon manager with catalog, signed capability grants, re-grant on update`

### Task 12: Per-addon settings forms

**Files:**
- Create: `src/app/pages/settings/AddonSettings.tsx`; test in `settings.test.tsx`
- Modify: the six addon modules (`save_settings` stores the form data in addon state and echoes it as `formData`)

**Behaviour:** `/settings/addon/$name` renders that addon's `settings` contribution (a `form` node, rjsf) inside
`AddonFrame` with the badge in the title. Saved values come back through the `addon` binding
(`{"$ref": "addon.settings"}`). A disabled addon's settings page shows "Enable <addon> to change its settings."
Non-owners see the form disabled.

- [ ] **Step 1: Failing test**

```tsx
it('saves an addon settings form and the value is in the addon state', async () => {
  const { user } = renderApp('/settings/addon/estimate')
  await user.selectOptions(await screen.findByLabelText(/Scale/), 'fibonacci') // rjsf renders enums as <select>
  await user.click(screen.getByRole('button', { name: 'Save' }))
  expect(await screen.findByText(/Settings saved/)).toBeInTheDocument()
  const state = await api.getAddonState(mockStore.workspaces[0].id, 'estimate')
  expect((state.settings as { scale: string }).scale).toBe('fibonacci')
})
it('a disabled addon asks to be enabled first', async () => {
  renderApp('/settings/addon/estimate', { setup: (s) => s.appendWs(s.workspaces[0].id, { type: 'addon.disabled', name: 'estimate' }) })
  expect(await screen.findByText(/Enable Estimate to change its settings/)).toBeInTheDocument()
})
```

(The `setup` option of `renderApp` is added in this task; Task 13 reuses it.)

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `settings: per-addon settings forms bound to addon state`

### Task 13: Disabled-addon behaviour everywhere

**Files:**
- Modify: `src/addon-ui/slots.tsx`, `src/app/pages/AddonPage.tsx`, `src/app/pages/ticket/Rail.tsx`,
  `src/app/pages/ticket/Raw.tsx`
- Create: `src/addon-ui/inactive.test.tsx`

**Behaviour:** when an addon is disabled, needs a grant or is uninstalled in the current workspace: no nav entry, no
slots, no palette commands, no Today cards, no board lanes or card fields. On a ticket that has `addons.<name>` data,
the rail shows a collapsed, greyed "<Addon> · inactive" item (still with the A badge) that reveals the raw data
read-only. A direct addon URL shows "<Addon> is not enabled in <workspace>." with a link to the addon manager for the
owner.

- [ ] **Step 1: Failing tests**

These use `renderApp`'s `setup` option (added in Task 12).

```tsx
const disable = (name: string) => (s: MockStore) => s.appendWs(s.workspaces[0].id, { type: 'addon.disabled', name })

it('hides every contribution of a disabled addon', async () => {
  renderApp('/board', { setup: disable('github') })
  await screen.findByRole('heading', { name: 'Board' })
  expect(screen.queryByRole('link', { name: /Code reviews/ })).toBeNull()
  expect(document.querySelector('[aria-label="From addon: github"]')).toBeNull()
})
it('shows inactive addon data on a ticket', async () => {
  const { user } = renderApp('/ticket/DEMO-0043', { setup: disable('estimate') })
  const item = await screen.findByRole('button', { name: /Estimate · inactive/ })
  await user.click(item)
  expect(await screen.findByText(/"points"/)).toBeInTheDocument()
})
it('a direct URL to a disabled addon page explains and links to the manager', async () => {
  renderApp('/addon/wiki/pages', { setup: disable('wiki') })
  expect(await screen.findByText(/Wiki is not enabled in/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Open the addon manager' })).toBeInTheDocument()
})
```

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `addons: consistent disabled and inactive behaviour across all surfaces`

### Task 14: Viewer and error polish for iteration-1 pages

The new pages set a standard (reasons on disabled controls, toasts for 403s, empty states). Bring Today, Board and
Ticket in line.

**Files:** `src/app/pages/{today,board,ticket}/*`, tests next to them.

**Behaviour:**
- Every mutation error shows a toast with the API `message` and `hint` (one shared helper
  `toastApiError(err)` in `src/app/toast.ts`; replace ad-hoc handling).
- Every disabled control has a tooltip reason.
- Board: an empty column says "Nothing here" and, for backlog, "Create a ticket (c)".

- [ ] **Step 1: Failing tests:** one per page: Tom sees a disabled "Claim" button with tooltip "Viewers cannot
  change tickets." on DEMO-0043; dragging a card to "done" toasts "Done is reached by a verdict"; an empty board
  column shows "Nothing here" (use the CLI workspace, which has few tickets, via the switcher).
- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `ux: shared API error toasts, reasons on disabled controls, empty states`

### Task 15: Full workspace switcher

**Files:**
- Create: `src/app/shell/WorkspaceSwitcher.tsx`, test in `shell.test.tsx`
- Modify: `src/app/shell/Sidebar.tsx`, `src/app/workspace.tsx`

**Behaviour:**
- Popover listing every workspace: prefix tile, name, your role, needs-you count (brand teal pill), the top 2
  needs-you items as one-line previews (click goes straight to that ticket in that workspace), relay status dot.
- `⌘1`–`⌘9` switch to the n-th workspace. The palette's Workspace group uses the same list.
- **Switching keeps the page:** `/board`, `/tickets` (filters kept), `/agents`, `/settings/*` stay. `/ticket/:key`
  goes to `/tickets` with a toast "DEMO-0043 is in Acme energy data". `/addon/:name/:page` stays when the addon
  is enabled in the target workspace; otherwise it goes to Today with a toast.
- The workspace id persists across reloads (localStorage, try/catch).

- [ ] **Step 1: Failing tests**

```tsx
it('shows needs-you previews per workspace and opens one directly', async () => {
  const { user } = renderApp('/')
  await user.click(await screen.findByRole('button', { name: 'Switch workspace' }))
  const int = await screen.findByRole('group', { name: /INT/ })
  await user.click(within(int).getAllByRole('link')[0])
  expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(/INT-/)
})
it('keeps the board when switching', async () => {
  const { user } = renderApp('/board')
  await screen.findByRole('heading', { name: 'Board' })
  await user.keyboard('{Meta>}2{/Meta}')
  expect(await screen.findByRole('heading', { name: 'Board' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Switch workspace' })).toHaveTextContent(/Internal/)
})
it('leaves a ticket page for the list when switching', async () => {
  const { user } = renderApp('/ticket/DEMO-0043')
  await screen.findByRole('heading', { level: 1 })
  await user.keyboard('{Meta>}2{/Meta}')
  expect(await screen.findByRole('table', { name: 'Tickets' })).toBeInTheDocument()
})
```

- [ ] **Step 2–4:** run (FAIL), implement, run (PASS).
- [ ] **Step 5: Commit** `shell: full workspace switcher with needs-you previews, page kept on switch`

### Task 16: Iteration 2 gate

- [ ] Full suite ×3 (no flakes), `npm run typecheck`, `npm run build`.
- [ ] Opus whole-phase review (`superpowers:requesting-code-review`) on Tasks 5–15; fix Critical and Important
  findings.
- [ ] Update the preview (see "Publishing the preview" at the end), push, and append an "Iteration 2 done" entry to
  `DECISIONS-LOG.md`.

---

# Phase 2: Iteration 3 (every addon)

Every addon task follows the same recipe, so each task below lists only what differs:

1. **Manifest** in `src/mocks/fixtures/addons.json` (or `catalog.json` when it starts uninstalled): `name`, `title`,
   `version`, `description`, `capabilities`, `contributions` (declarative nodes binding to `addon.*`, `ticket.*`,
   `workspace.*`), `commands`, `decisions`.
2. **Mock module** `src/mocks/addons/<name>.ts`: `seed`, `view`, `actions`. Register it.
3. **Tests** `src/mocks/addons/<name>.test.ts` (state and actions through `api`) and
   `src/app/addons/<name>.test.tsx` (the page rendered via `renderApp('/addon/<name>/<page>')`).
4. Everything the addon contributes carries the A badge (automatic through `AddonFrame`), and every human decision
   goes through `AddonDecision` (core-rendered on Today and the ticket rail).
5. Commit `addon(<name>): …`.

Whether an addon starts **installed** or in the **catalog** is set per task, so the addon manager has something to
install during review.

### Task 17: publish (apps and shares)

- Starts installed. Capabilities `serve_http`, `network`.
- **State:** `apps` (3: "Billing explorer" (Streamlit, running, 2 recipients), "Energy dashboard" (static,
  stopped), "Ops notebook" (failed build, log)); `shares` (5: public link, secret link, sealed to Mara; with expiry,
  views and last viewer); `settings` (default expiry 7 days, namespace `acme`).
- **Pages:** `nav/shares` "Apps & shares": tabs via a `stack` of sections. Apps table with rowActions
  Start/Stop/Logs/Redeploy; Shares list with actions Copy link / Extend 7 days / Revoke; a "Show-once link" flow:
  action `share_once` returns `message` "Link copied, shown once: https://p.acme.example/s/…".
- **Ticket panel:** shares of this ticket + "Share report…" button. **Today card:** "2 apps running · 1 failed build".
- **Decision:** `dec_publish_report` (existing) plus `dec_publish_failed_build` ("Ops notebook failed to build:
  retry with the last good version?").
- **Tests:** revoking a share removes it from state and from the ticket panel; `start` on a stopped app flips
  status to `running`; the failed-build decision appears on Today for Severin and not for Tom.

### Task 18: github (reviews and the issues lane)

- Starts installed (update 0.6.0 pending from Task 11).
- **State:** `prs` (6 across 2 repos: checks pass/fail/pending, review state, linked ticket, author agent or person,
  diff stats), `issues` (8 open, importable), `settings` (repos, poll interval).
- **Pages:** `nav/reviews` "Code reviews": PR table (repo, #, title, ticket link, checks chip, reviews, updated)
  with rowActions "Approve on GitHub" (mock: marks review approved) and "Open". **Board lane** `issues` keeps Import
  (now with an item action instead of a special case). **Ticket panel** `pr`: branch, PR, checks list (kv), "Refresh".
- **Command:** `refresh` updates one PR's checks from pending to pass (state change visible).
- **Tests:** import creates a backlog ticket with a link to the issue, and removes the issue from the lane;
  refresh flips a pending check; the PR panel is hidden on tickets without a PR (`when`).

### Task 19: usage

- Starts installed.
- **State:** daily cost and tokens per model for 30 days (deterministic generator), per-ticket totals, per-agent
  totals, `settings.budget_chf` 150.
- **Page:** `nav/overview`: stats (this week, month to date, budget used as `progress`), line chart per day, bar
  chart per model, table of the top tickets by cost. `alert` warn tone when the budget passes 80%.
- **Ticket panel:** cost, tokens, sessions and duration for the ticket. **Today card:** "This week CHF 31.40".
- **Tests:** the budget alert appears after `save_settings` with `budget_chf` 30; the per-ticket panel for
  DEMO-0043 shows CHF and tokens.

### Task 20: wiki **[security review: markdown]**

- Starts installed.
- **State:** 7 pages (title, slug, markdown, updated, by, linked tickets), one with a code block and a table.
- **Pages:** `nav/pages` "Wiki": list of pages with search; `nav/page` is not possible with fixed nav ids, so use a
  `frame`-free approach: the list items carry action `open` with `{ slug }`, which sets `state.current`; the page
  binds `markdown` to `addon.current.markdown`. Editing: a `form` node (title, markdown textarea) with action `save`.
- **Ticket panel** `related`: pages linking to the ticket, plus "Link page…" (form with a select of pages).
- **Rendering:** `SafeMarkdown` only; a page containing `<script>` and `javascript:` links renders them inert (test).
- **Tests:** open → edit → save shows the new text; the script/`javascript:` page is inert; related pages appear
  on DEMO-0043.

### Task 21: terminals (xterm.js over a fake PTY) **[security review]**

- Starts installed. Capability `pty`.
- **Dependency:** `npm i @xterm/xterm@<latest 5.x, exact>`; import its CSS in `TerminalView`. Lazy-load the whole
  terminal module (`React.lazy`) so the main bundle does not grow.
- **Files:** `src/app/terminal/{TerminalView.tsx,fakePty.ts,fakePty.test.ts,terminal.test.tsx}`.
- **fakePty:** a tiny line-based shell. Prompt `severin@acme ~/energy (feat/billing-join) $ `. Commands: `orch status`
  (prints the §10.5 example output with live data), `orch show DEMO-0043 --section current_state`, `orch task next`,
  `git status`, `git log --oneline -5`, `ls`, `pwd`, `clear`, `help`, `exit`; arrow-up history; Ctrl-C; unknown
  commands print `zsh: command not found: <x>`. `orch approve …` prints
  `err human_only approve · retry:false · next: orch ask or orch wait` **when the session belongs to an agent**.
- **State:** sessions (3: Severin's shell in DEMO-0043's worktree, an agent's read-only mirror labelled "agent:
  claude-code (read only, no typing)", a stopped one), `settings` (shell, font size).
- **Pages:** `nav/sessions`: session list with actions Open/Close and "New terminal"; opening shows a `terminal` node.
  **Ticket panel:** "Open terminal in this ticket's worktree". **Command:** `open`.
- **Rules shown in the UI:** "Terminals are never granted to agents"; agent sessions are view-only.
- **Tests:** `fakePty` unit tests (each command's first line, history, unknown command); the page opens a session and
  renders an element with `role="textbox"` from xterm; the agent mirror has no input (`aria-readonly`).

### Task 22: estimate

- Already present: keep the card field, panel form and settings.
- Add: board column header sums ("13 pts") via a new `board.lane`-free approach: the **Board** reads
  `board.card_field` values and shows a sum per column when the field is numeric (core feature, badge on the sum).
  Settings: scale (`fibonacci`, `linear`, `t-shirt`) changes the panel form's enum.
- **Tests:** the column sum updates after estimating a card; switching the scale to t-shirt shows S/M/L in the form.

### Task 23: worktrees

- Starts in the catalog (installed during review). Capabilities none beyond core (`git` runs in the host).
- **State:** per repo the worktrees (path, branch, ticket, dirty files count, ahead/behind, created by).
- **Page** `nav/worktrees`: table per repo with rowActions "Open terminal here" (navigates via the terminals command
  when terminals is enabled), "Remove" (refused with message when dirty: "3 changed files. Commit or stash first.").
  "Add worktree" form (ticket, repo, base branch).
- **Ticket panel:** the ticket's worktrees and "Add worktree for this ticket".
- **Tests:** add creates `wt/<ticket>-<repo>` on branch `feat/<ticket-slug>`; remove refuses when dirty and works
  when clean.

### Task 24: quick tasks

- Starts in the catalog. Behaviour from v1 `quick-tasks.md`.
- **State:** 6 quick tasks `Q-001…`: open, claimed by an agent, done with one line of proof, and one **outgrew**
  (4 commits, 7 files > limits 1 and 3). `settings`: agents may add (off), most commits 1, most files 3.
- **Page** `nav/quick`: an "Add a quick task" one-line form at the top; list with status chips and item actions
  (Claim, Close with proof, Make a ticket). **Decision:** the outgrown task produces `AddonDecision`
  "Q-004 outgrew its limit: make it a ticket, or allow 3 more files?" (core-rendered on Today).
- **Tests:** add appends Q-007 open; "Make a ticket" creates a backlog chore with the line as title and marks the
  quick task `converted`; the outgrew decision shows on Today and, once decided, disappears.

### Task 25: records

- Starts in the catalog. What it does (v1 C29): commits and pushes orch's ticket records to git.
- **State:** pending record changes (per ticket: events since the last record commit), last push (time, commit,
  remote), `settings` (auto-commit every N minutes, push on/off, remote).
- **Page** `nav/records`: "12 events on 4 tickets not recorded yet", table per ticket, buttons "Commit records"
  and "Push"; a history list of record commits with short hashes. A push failure scenario (`alert` error: "Remote
  rejected: non-fast-forward. Pull first.") when pushing twice within the mock without a pull; "Pull" resolves it.
- **Tests:** commit empties the pending list and adds a history entry; push, push again → error alert; pull,
  push → ok.

### Task 26: activity

- Starts in the catalog. v1 D11: activity, timeline, agents.
- **Page** `nav/activity`: workspace-wide timeline grouped by day: every ticket and workspace event, with
  filters (people, agents, type groups: status, gates, questions, tasks, artifacts, addons) as list items with
  badges; a "by ticket" table (events today, last actor). The page reads `addon.timeline`, which the module's `view`
  builds from `store.eventsOf` over the workspace tickets plus `wsEventsOf`.
- **Today card:** "Today: 23 events, 6 by agents".
- **Tests:** filtering to "gates" shows only gate events; an action elsewhere (e.g. a comment on DEMO-0043) appears
  at the top of the timeline after the live update.

### Task 27: widgets (rich ticket sections) **[security review]**

- Starts installed. Format from v1 `widgets.md` (`orch.widgets.v1`): a fenced block with the info string `orch`
  holding one JSON object with exactly one of `type` (core type), `widget` (template `name@version`), or `html`
  (artifact path + sha256).
- **Files:** `src/app/pages/ticket/widgets/{parse.ts,parse.test.ts,CoreWidget.tsx,WidgetBlock.tsx}`; modify
  `Overview.tsx` (section rendering), fixtures (DEMO-0043 sections get 3 widgets: a `bars` "Bundle size, kB", a
  `checks` table, a `template` before/after slider; DEMO-0041 gets an `html` prototype with a sha256 that matches,
  and one with a mismatching sha256).
- **Core types** (rendered by core, no script): `bars`, `table`, `checks`, `kv`. **Template and html** render
  through the `frame` node (Task 3) inside `AddonFrame` for the widgets addon. A wrong sha256, duplicate `id`,
  unknown top-level keys or invalid JSON render the block as code with a one-line reason.
- **Tests:** parser unit tests for each refusal rule; the Overview renders the bars widget as an SVG with a
  `<title>`, the template as a sandboxed frame, and the sha-mismatch as code with "sha256 does not match".

### Task 28: start agent (plus model-routing)

- **start agent** starts installed; **model-routing** starts in the catalog.
- **start agent** (capability `spawn_agent`, owner-granted): ticket panel and `nav/start` page. Form: mode (Refine,
  Work on ticket, Fix failing checks, Continue after feedback), harness (Claude Code, Codex), where (Terminals or
  background), and a preview of the exact command (`code` node). "Start" (action `start`) asks core for a
  confirmation (an `AddonDecision`-style core dialog, signed when no grant is active) and then **plays a simulator
  script** (Task 4) on that ticket: `claim.taken` → `task.started T1` (lease) → `log.added` → `task.done T1` with a
  receipt → `question.asked` (blocking, to the viewer) → waits. The run is visible live on the ticket, Today ("agents
  at work"), Agents and Activity. Stop: action `stop` ends the script and releases the claim.
- **model-routing** (capability `launch`): settings form (Light/Standard/Strong model names, tier per mode, subagent
  model). When enabled, the start-agent preview shows the line
  `Model · work runs on standard: Standard (sonnet); subagents on haiku`, and the command includes `--model sonnet`.
  Escalation decision: when a task failed verify in two sessions (seeded on DEMO-0045 T2), Today shows "T2 failed its
  check in two sessions: start the next session on Strong?" (core-rendered). Warning `alert` when a setting is not a
  model name.
- **Tests:** starting on DEMO-0044 with fake timers produces claim → task done → a blocking question addressed to
  Severin on Today; stop releases the claim; with model-routing on, the preview shows `--model`; an invalid model
  name blocks Start with the sentence.

### Task 29: guide

- Starts installed. v1 D19 "Guide, new text".
- **Content:** 8 short pages in fixtures (markdown): Getting around, Tickets and sections, Gates and approvals,
  Agents and grants, Questions, Addons and the orange A, Keyboard shortcuts (generated from the shortcut registry so it
  never goes stale), What is simulated in this mockup.
- **UI:** `nav/guide` page (list + content like the wiki), and core's `?` shortcut opens a "Help" sheet showing
  the guide page that matches the current route (route → page map in the module's `view`).
- **Route map:** `/` → Getting around, `/board` and `/tickets*` → Tickets and sections, `/ticket/*` → Gates and
  approvals, `/agents` → Agents and grants, `/settings/addons` and `/addon/*` → Addons and the orange A, anything
  else → Getting around.
- **Tests:** `?` on the board opens the Help sheet with the heading "Tickets and sections"; `?` inside a text input
  types a `?` instead; the Keyboard shortcuts page lists `g b`.

### Task 30: AI Factory (Phase 2 preview) and schedules (later)

Both start in the catalog and carry a **"Preview"** chip next to the badge (they are not in the P2 scope).
- **AI Factory** (v1 `factory.md`): `nav/factory`. One factory epic (DEMO-0050 "Monthly billing v2") with the
  charter limits (25 children or 72 h; children ≤ size m), `progress` for children and time used, children table
  (status, auto-approved by agent, size), **permits** list (agent asks, human grants: core-rendered decisions with
  "Grant once / Grant for this epic / Refuse"), the Ready report (markdown) and a Stopped state (`alert`). "Pause
  factory" and "Resume" (signed). A simulator script adds a child and a permit request every ~20 s while the page is
  open.
- **Schedules** (v1 `schedules.md`): `nav/schedules`: three kinds (schedule, listener, recurring) with when/on,
  skill, armed state, last run, next run; Arm/Disarm (signed) and "Run now"; run history with reports; a recurring
  ticket finding appears on Today ("Dependency update · Monday: file it?") as a core decision.
- **Tests:** a permit decision granted once disappears and logs `permit.granted` on the epic; pausing shows the
  paused state; arming a schedule shows its next run; "Run now" adds a run with a report.

### Task 31: Iteration 3 gate, bundle and accessibility

**Files:** `vite.config.ts` (manual chunks), `src/app/router.tsx` (lazy routes), `src/test/a11y.test.tsx`,
`package.json` (`axe-core` exact, dev).

- [ ] Code-split: every page and every addon renderer that pulls a heavy dependency (shiki, recharts, xterm, rjsf)
  is lazy. Target: no chunk warning; main chunk < 350 kB minified.
- [ ] **a11y smoke test** (`src/test/a11y.test.tsx`): for each route in a list (Today, Board, Tickets, New ticket,
  Ticket DEMO-0043 each tab, Agents, each Settings tab, each addon page), render with `renderApp`, wait for the h1,
  run `axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } })` (jsdom can't compute contrast),
  and expect no `serious` or `critical` violations.
- [ ] **Layout test:** at `window.innerWidth = 1024` with the wide sidebar, `document.documentElement.scrollWidth <= 1024`
  on each route (jsdom can't lay out: implement this one in the Playwright pass in Task 32 instead, and keep the
  vitest version only for "no fixed min-width > 1024 in className" via a grep test).
- [ ] **Orange guard:** a test that greps `src/**/*.tsx` outside `src/addon-ui/` for the orange token / `orange-`
  classes and fails on any hit.
- [ ] Full suite ×3, typecheck, build. Opus whole-phase review of Tasks 17–30. Fix Critical/Important. Push.

---

# Phase 3: UX and UI testing

### Task 32: Scripted walkthroughs in a real browser

Use the Playwright MCP tools (`mcp__plugin_playwright_playwright__*`) against `npm run dev`. Run each scenario at
**1024×768 (rail auto)** and **1440×900 (wide)**. Save screenshots to
`addons/dashboard/.ux/<scenario>/<step>-<width>.png` (add `.ux/` to `.gitignore`: screenshots stay local).

Scenarios (each a numbered step list in `REVIEW.md`, written first, then executed):
1. **Owner morning:** Today → answer Q2 on DEMO-0043 → approve the plan gate (sign) → decide the publish decision →
   board → move a card.
2. **New work:** `c` → create a bug with requirements → find it with ⌘K → start an agent on it → watch it claim, finish
   T1 and ask a question → answer it from Today.
3. **Verdict:** open a ticket in testing → review evidence (artifacts, receipts) → give a fail verdict with text →
   it returns to in-progress.
4. **Maintainer (Mara):** switch the viewer → see what differs (cannot sign the owner-only gates, cannot change
   settings) → approve what she may.
5. **Viewer (Tom):** every page read-only, reasons visible, no dead ends.
6. **Owner admin:** add a member, change a gate policy, install quick tasks (grant, enable), update github (re-grant),
   revoke an agent grant.
7. **Addons tour:** every addon page, ticket panel and Today card; terminal session commands; wiki edit; records
   push failure and recovery; factory permit; schedule arm.
8. **Workspace hopping:** switcher previews, ⌘2 on the board, a ticket page switch, a disabled-addon page switch.
9. **Keyboard only:** scenario 1 and 2 without the mouse (Tab, `g x`, `j/k`, ⌘K, ⌘↵).

For every step check and note: the page answers the user's next question without hunting; feedback within 300 ms
(toast, optimistic change or spinner); no horizontal scroll; focus visible and in a sensible order; text not
truncated without a tooltip; empty, loading and error states present; the orange A only on addon UI; consistent
wording (one name per thing: "Ticket", "Grant", "Approve").

Also run `lighthouse_audit` (chrome-devtools MCP, accessibility category) on Today, Ticket, Settings → Addons and one
addon page. Target ≥ 95, with contrast included this time.

Write every finding to `addons/dashboard/.ux/findings.md` as `[severity] scenario/step — what — expected — screenshot`.
Severity: **blocker** (cannot finish the task), **major** (confusing or wrong), **minor** (polish).

### Task 33: Heuristic review

Dispatch one **Opus** reviewer with: the screenshots folder, `REVIEW.md`, `findings.md`, the HANDOVER decisions, and
Nielsen's 10 heuristics. It adds findings it can justify from the screenshots and code (not taste), merges
duplicates, and ranks them. Its output replaces `findings.md`.

### Task 34: Fix rounds

- Group findings by page. One Sonnet task per group, each fixing blockers and majors with a test that pins each
  fix (role/name assertions, not snapshots). Minors: fix when under ~10 minutes each; the rest go to
  `REVIEW.md` "Known polish items".
- Re-run the affected Task 32 scenarios. Repeat until there are **no blockers and no majors**, at most 3 rounds;
  anything left after round 3 is listed in `REVIEW.md` with a reason.

### Task 35: Review kit for the owner

**Files:** `src/app/review/ReviewTour.tsx` (+ test), `REVIEW.md`, `HANDOVER.md`, `DECISIONS-LOG.md`.

- [ ] **Review tour:** the "Demo data" pill gets "Review tour". A sheet lists the scenarios from `REVIEW.md` as
  checklists; each step has a "Go" button that navigates (and switches viewer or workspace when needed). Ticks are
  kept per browser (localStorage, try/catch), with "Reset demo data and ticks".
  Test: clicking "Go" on "Owner admin · Install quick tasks" lands on `/settings/addons`.
- [ ] **REVIEW.md:** how to open the preview, the scenarios, what is simulated (Touch ID, PTY, GitHub, relay,
  agent runs, time), known polish items, open questions for the owner (from `DECISIONS-LOG.md`).
- [ ] **HANDOVER.md:** rewrite "What exists" and "Next" for the post-mockup state (backend swap: list every endpoint
  the mock serves, grouped, as the contract for the FastAPI host).
- [ ] Full suite ×3, typecheck, build, publish the preview, push. Final message to the owner: the preview link,
  `REVIEW.md` highlights and the decisions to confirm.

---

## Publishing the preview

At the end of each phase (procedure from HANDOVER.md):
1. `npm run build`.
2. Copy `dist/assets` into a preview folder in the session scratchpad.
3. Write the small `index.html`: `<title>`, the Google Fonts link, the built CSS, a `<style>` with the dark
   background, a script adding the `dark` class, `<div id="root">`, the built module script.
4. Replace every literal U+FFFD in the JS bundles with the JS escape `�` (the publisher refuses the literal
   character; the occurrences are inside template strings). With lazy chunks this now applies to **every** `.js`
   file, not only the main one.
5. Publish to the same URL (https://claude.ai/artifact/MQWNJj1NZJ9CGCCibHCnmx). Pass the new hashed files in
   `files` and `null` for the old ones.

## Execution notes for the manager

- Order is fixed: Phase 0 → 1 → 2 → 3. Within Phase 2, Tasks 17–30 only depend on Phase 0–1 and on Task 21
  (terminal node) for Task 23's "Open terminal here"; they may run in parallel worktrees, two at a time, because
  each touches its own addon module, fixture entry and tests. `addons.json` / `catalog.json` edits are the merge
  hotspot: each task adds its own object, never reformats the file.
- Each implementer gets: this plan's Global Constraints, its task, and the Interfaces of the tasks it consumes.
- Stop rule for the run: a task that fails review three times is parked with a note in `DECISIONS-LOG.md`; the run
  continues with the next task.
