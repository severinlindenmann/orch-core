import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'

const setup = (viewer = 'p_sev', opts: { terminals?: boolean } = {}) => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'worktrees')
  if (opts.terminals === false) store.addonOp(ws, 'terminals', { op: 'disable' }, { kind: 'person', id: 'p_sev' })
  store.setViewer(viewer)
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type S = ReturnType<typeof setup>
interface Wt {
  id: string
  path: string
  branch: string
  repo: string
  ticket: string
  dirty: number
  ahead: number
  behind: number
  created_by: string
}
interface Item {
  title: string
  subtitle?: string
  badge?: string
  actions?: { action: string; label: string; args?: Record<string, unknown> }[]
}
type State = { worktrees: Wt[]; rowsByRepo: Record<string, { id: string; path: string }[]>; rowActions: { label: string; args?: Record<string, unknown> }[]; byTicket: Record<string, Item[]>; terminalsActive: boolean; addSchema: { properties: { ticket: { enum: string[] }; repo: { enum: string[] } } } }
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'worktrees')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'worktrees', id, body)

describe('worktrees state', () => {
  it('seeds worktrees for DEMO tickets only: one dirty with 3 files, one ahead and behind', async () => {
    const s = setup()
    const st = await state(s)
    expect(st.worktrees.length).toBeGreaterThanOrEqual(3)
    expect(st.worktrees.every((w) => w.ticket.startsWith('DEMO-'))).toBe(true)
    expect(st.worktrees.find((w) => w.dirty === 3)).toBeTruthy()
    expect(st.worktrees.find((w) => w.ahead > 0 && w.behind > 0)).toBeTruthy()
  })
  it('other workspaces start with none', async () => {
    const store = createMockStore({ persist: false })
    const other = store.workspaces.find((w) => w.prefix !== 'DEMO')!.id
    installAndGrant(store, other, 'worktrees')
    expect(store.addonState(other, 'worktrees').worktrees).toEqual([])
  })
  it('lists the add form choices: open DEMO tickets and the repos', async () => {
    const s = setup()
    const p = (await state(s)).addSchema.properties
    expect(p.ticket.enum).toContain('DEMO-0043')
    expect(p.ticket.enum).not.toContain('DEMO-0042') // done
    expect(p.ticket.enum.every((k) => k.startsWith('DEMO-'))).toBe(true)
    expect(p.repo.enum).toContain('acme-energy/energy-dbt')
  })
  it('groups rows per repo and per ticket for the page and the ticket panel', async () => {
    const s = setup()
    const st = await state(s)
    expect(st.rowsByRepo['energy-dbt'].length).toBeGreaterThan(0)
    expect(st.byTicket['DEMO-0043'][0].title).toBe('wt/DEMO-0043-energy-dbt')
  })
})

describe('worktrees add', () => {
  it('creates wt/<ticket>-<repo> on feat/<ticket-slug> from the base branch', async () => {
    const s = setup()
    const r = await run(s, 'add', { formData: { ticket: 'DEMO-0044', repo: 'acme-energy/billing-api', base: 'main' } })
    expect(r.changed).toBe(true)
    const w = (await state(s)).worktrees.find((x) => x.ticket === 'DEMO-0044')!
    expect(w).toMatchObject({ path: 'wt/DEMO-0044-billing-api', branch: 'feat/rotate-warehouse-service-credentials', repo: 'acme-energy/billing-api', dirty: 0, ahead: 0, behind: 0 })
  })
  it('slug is lowercase, dashes, at most 40 characters without a trailing dash', async () => {
    const s = setup()
    await run(s, 'add', { formData: { ticket: 'DEMO-0047', repo: 'acme-energy/ingest', base: 'main' } })
    const w = (await state(s)).worktrees.find((x) => x.ticket === 'DEMO-0047')!
    expect(w.branch).toMatch(/^feat\/[a-z0-9-]{1,40}$/)
    expect(w.branch.endsWith('-')).toBe(false)
    expect(w.branch).toBe('feat/spike-evaluate-dagster-vs-airflow-for')
  })
  it('the ticket panel form passes the ticket in the body', async () => {
    const s = setup()
    await run(s, 'add', { ticket: 'DEMO-0048', formData: { repo: 'acme-energy/ingest', base: 'main' } })
    expect((await state(s)).byTicket['DEMO-0048'][0].title).toBe('wt/DEMO-0048-ingest')
  })
  it('refuses a duplicate for the same ticket and repo', async () => {
    const s = setup()
    const n = (await state(s)).worktrees.length
    const r = await refused(run(s, 'add', { formData: { ticket: 'DEMO-0043', repo: 'acme-energy/energy-dbt', base: 'main' } }))
    expect(r).toMatchObject({ status: 409, code: 'worktrees.exists' })
    expect(r.message).toMatch(/already has a worktree/)
    expect((await state(s)).worktrees).toHaveLength(n)
  })
  it('refuses unknown repos, tickets of other workspaces and empty input', async () => {
    const s = setup()
    const n = (await state(s)).worktrees.length
    expect(await refused(run(s, 'add', { formData: { ticket: 'DEMO-0044', repo: 'evil/repo', base: 'main' } }))).toMatchObject({ status: 400, code: 'validation' })
    expect(await refused(run(s, 'add', { formData: { ticket: 'NOPE-1', repo: 'acme-energy/ingest', base: 'main' } }))).toMatchObject({ status: 404, code: 'not_found' })
    expect(await refused(run(s, 'add', { formData: {} }))).toMatchObject({ status: 400, code: 'validation' })
    expect((await state(s)).worktrees).toHaveLength(n)
  })
  it('defaults the base branch to main', async () => {
    const s = setup()
    await run(s, 'add', { formData: { ticket: 'DEMO-0044', repo: 'acme-energy/ingest' } })
    expect((await state(s)).worktrees.find((x) => x.ticket === 'DEMO-0044')).toMatchObject({ base: 'main' })
  })
  it('viewers cannot add', async () => {
    const s = setup('p_tom')
    await expect(run(s, 'add', { formData: { ticket: 'DEMO-0044', repo: 'acme-energy/ingest', base: 'main' } })).rejects.toMatchObject({ status: 403 })
  })
})

describe('worktrees remove', () => {
  it('refuses when dirty, with the file count', async () => {
    const s = setup()
    const dirty = (await state(s)).worktrees.find((w) => w.dirty === 3)!
    const r = await refused(run(s, 'remove', { id: dirty.id }))
    expect(r).toMatchObject({ status: 409, code: 'worktrees.dirty', message: '3 changed files.', hint: 'Commit or stash first.' })
    expect((await state(s)).worktrees.some((w) => w.id === dirty.id)).toBe(true)
  })
  it('removes a clean worktree', async () => {
    const s = setup()
    const clean = (await state(s)).worktrees.find((w) => w.dirty === 0)!
    const r = await run(s, 'remove', { id: clean.id })
    expect(r.changed).toBe(true)
    expect((await state(s)).worktrees.some((w) => w.id === clean.id)).toBe(false)
  })
  it('an unknown id changes nothing', async () => {
    const s = setup()
    expect(await refused(run(s, 'remove', { id: 'nope' }))).toMatchObject({ status: 404, code: 'not_found' })
  })
})

describe('worktrees open terminal here', () => {
  it('offers the action only while terminals is active', async () => {
    const on = await state(setup())
    expect(on.terminalsActive).toBe(true)
    expect(on.rowActions.map((a) => a.label)).toEqual(['Open terminal here', 'Remove'])
    const off = await state(setup('p_sev', { terminals: false }))
    expect(off.terminalsActive).toBe(false)
    expect(off.rowActions.map((a) => a.label)).toEqual(['Remove'])
  })
  it('opens a terminal session for the worktree ticket through the terminals module', async () => {
    const s = setup()
    const w = (await state(s)).worktrees.find((x) => x.ticket === 'DEMO-0041')!
    const r = await run(s, 'open_terminal', { id: w.id })
    expect(r.message).toMatch(/Terminal open in the DEMO-0041 worktree/)
    const t = (await s.api.getAddonState(s.ws, 'terminals')) as unknown as { sessionByTicket: Record<string, string> }
    expect(t.sessionByTicket['DEMO-0041']).toBeTruthy()
  })
  it('says so when terminals is not active and opens nothing', async () => {
    const s = setup('p_sev', { terminals: false })
    const w = (await state(s)).worktrees[0]
    const r = await refused(run(s, 'open_terminal', { id: w.id }))
    expect(r).toMatchObject({ status: 409, code: 'addon.inactive' })
    expect(r.message).toMatch(/Terminals is not active/)
  })
})

describe('worktrees open terminal here is authorized by terminals', () => {
  const sessions = async (s: S) => ((await s.api.getAddonState(s.ws, 'terminals')) as unknown as { sessions: unknown[] }).sessions.length
  it('a viewer cannot open a terminal through it', async () => {
    const s = setup('p_tom')
    await expect(run(s, 'open_terminal', { id: 'wt/DEMO-0043-energy-dbt' })).rejects.toMatchObject({ status: 403 })
  })
  it('a member with terminals active creates a session', async () => {
    const s = setup('p_mara')
    const before = await sessions(s)
    await run(s, 'open_terminal', { id: 'wt/DEMO-0041-energy-dbt' })
    expect(await sessions(s)).toBe(before + 1)
  })
  it('does not create a session when terminals is disabled', async () => {
    const s = setup('p_sev', { terminals: false })
    const before = (s.store.addonState(s.ws, 'terminals').sessions as unknown[]).length
    await refused(run(s, 'open_terminal', { id: 'wt/DEMO-0041-energy-dbt' }))
    expect((s.store.addonState(s.ws, 'terminals').sessions as unknown[]).length).toBe(before)
  })
})

describe('worktrees and restricted tickets', () => {
  const restrict = (s: S) => {
    ;(s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get('DEMO-0041')!.visibility = { restricted: ['p_sev'] }
  }
  it('a member outside the restricted list does not see the worktree and cannot remove it', async () => {
    const s = setup('p_mara')
    restrict(s)
    const st = await state(s)
    expect(st.worktrees.some((w) => w.ticket === 'DEMO-0041')).toBe(false)
    expect(Object.values(st.rowsByRepo).flat().some((i) => i.path.includes('DEMO-0041'))).toBe(false)
    expect(st.byTicket['DEMO-0041']).toBeUndefined()
    expect(await refused(run(s, 'remove', { id: 'wt/DEMO-0041-energy-dbt' }))).toMatchObject({ status: 404, message: 'No such worktree.' })
    expect(await refused(run(s, 'open_terminal', { id: 'wt/DEMO-0041-energy-dbt' }))).toMatchObject({ status: 404, message: 'No such worktree.' })
  })
  it('a listed person sees it and can remove it', async () => {
    const s = setup('p_sev')
    restrict(s)
    expect((await state(s)).byTicket['DEMO-0041']).toHaveLength(1)
    expect((await run(s, 'remove', { id: 'wt/DEMO-0041-energy-dbt' })).changed).toBe(true)
  })
})
