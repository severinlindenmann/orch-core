import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'quick')
  store.setViewer(viewer)
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type S = ReturnType<typeof setup>
interface Q {
  id: string
  title: string
  status: string
  claimed_by?: string
  proof?: string
  commits?: number
  files?: number
  ticket?: string
}
interface Item {
  title: string
  subtitle?: string
  badge?: string
  actions?: { label: string; action: string; args?: Record<string, unknown> }[]
}
type State = { items: Q[]; list: Item[]; settings: { agents_add: boolean; max_commits: number; max_files: number }; closePanel: { type: string } }
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'quick')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'quick', id, body)
const decisions = async (s: S) => (await s.api.getAddonDecisions(s.ws)).filter((d) => d.addon === 'quick')

describe('quick tasks seed', () => {
  it('seeds six tasks Q-001..Q-006: open, claimed, done with proof, one outgrew', async () => {
    const st = await state(setup())
    expect(st.items.map((q) => q.id)).toEqual(['Q-001', 'Q-002', 'Q-003', 'Q-004', 'Q-005', 'Q-006'])
    const by = (status: string) => st.items.filter((q) => q.status === status)
    expect(by('open').length).toBeGreaterThanOrEqual(2)
    expect(by('claimed')[0].claimed_by).toBeTruthy()
    expect(by('done')[0].proof).toMatch(/\S/)
    expect(by('outgrew')).toHaveLength(1)
    expect(by('outgrew')[0]).toMatchObject({ id: 'Q-004', commits: 4, files: 7 })
  })
  it('settings: agents may add off, most commits 1, most files 3', async () => {
    expect((await state(setup())).settings).toEqual({ agents_add: false, max_commits: 1, max_files: 3 })
  })
  it('the outgrew task shows the limits it went over; actions follow the status', async () => {
    const st = await state(setup())
    const o = st.list.find((i) => i.title.includes('Q-004'))!
    expect(o.subtitle).toMatch(/4 commits, 7 files/)
    expect(o.subtitle).toMatch(/limit of 1 commit and 3 files/)
    expect(o.actions!.map((a) => a.label)).toEqual(['Make a ticket'])
    const labels = (needle: string) => st.list.find((i) => i.title.includes(needle))!.actions?.map((a) => a.label)
    expect(labels('Q-001')).toEqual(['Claim', 'Make a ticket'])
    expect(labels('Q-003')).toEqual(['Close with proof'])
    expect(labels('Q-005')).toBeUndefined()
  })
})

describe('quick tasks actions', () => {
  it('add appends Q-007 as open', async () => {
    const s = setup()
    const r = await run(s, 'add', { formData: { title: 'Remove the unused env var' } })
    expect(r.changed).toBe(true)
    const q = (await state(s)).items.at(-1)!
    expect(q).toMatchObject({ id: 'Q-007', title: 'Remove the unused env var', status: 'open' })
  })
  it('add refuses an empty or multi-line line', async () => {
    const s = setup()
    expect((await run(s, 'add', { formData: { title: '  ' } })).changed).toBeFalsy()
    expect((await run(s, 'add', { formData: { title: 'a\nb' } })).changed).toBeFalsy()
    expect((await state(s)).items).toHaveLength(6)
  })
  it('claim marks an open task as claimed by the viewer and refuses a claimed one', async () => {
    const s = setup()
    expect((await run(s, 'claim', { id: 'Q-001' })).changed).toBe(true)
    expect((await state(s)).items.find((q) => q.id === 'Q-001')).toMatchObject({ status: 'claimed', claimed_by: 'Severin' })
    expect((await run(s, 'claim', { id: 'Q-003' })).changed).toBeFalsy()
  })
  it('close with proof needs a claimed task and a one-line proof', async () => {
    const s = setup()
    await run(s, 'start_close', { id: 'Q-003' })
    expect((await run(s, 'close', { formData: { proof: '' } })).changed).toBeFalsy()
    const r = await run(s, 'close', { formData: { proof: 'removed, 9ac1f20' } })
    expect(r.changed).toBe(true)
    expect((await state(s)).items.find((q) => q.id === 'Q-003')).toMatchObject({ status: 'done', proof: 'removed, 9ac1f20' })
    expect((await run(s, 'close', { formData: { proof: 'again' } })).changed).toBeFalsy()
  })
  it('Make a ticket creates a backlog chore with the line as title and marks the task converted', async () => {
    const s = setup()
    const r = await run(s, 'make_ticket', { id: 'Q-001' })
    expect(r.changed).toBe(true)
    const q = (await state(s)).items.find((x) => x.id === 'Q-001')!
    expect(q.status).toBe('converted')
    expect(q.ticket).toMatch(/^DEMO-\d{4}$/)
    const t = await s.api.getTicket(q.ticket!)
    expect(t).toMatchObject({ type: 'chore', status: 'backlog', title: q.title })
    expect(t.body.requirements).toBe(q.title)
  })
  it('Make a ticket is refused for claimed, done and converted tasks', async () => {
    const s = setup()
    for (const id of ['Q-003', 'Q-005']) expect((await run(s, 'make_ticket', { id })).changed).toBeFalsy()
    await run(s, 'make_ticket', { id: 'Q-001' })
    expect((await run(s, 'make_ticket', { id: 'Q-001' })).changed).toBeFalsy()
  })
  it('a viewer cannot add, claim or make a ticket', async () => {
    const s = setup('p_tom')
    for (const [id, body] of [['add', { formData: { title: 'Nope' } }], ['claim', { id: 'Q-001' }], ['make_ticket', { id: 'Q-001' }]] as const)
      await expect(run(s, id, body)).rejects.toMatchObject({ status: 403 })
  })
  it('only an owner saves settings', async () => {
    const s = setup('p_mara')
    await expect(run(s, 'save_settings', { formData: { agents_add: true, max_commits: 2, max_files: 4 } })).rejects.toMatchObject({ status: 403 })
  })
})

describe('quick tasks outgrew decision', () => {
  it('shows on Today for a maintainer, not for a viewer', async () => {
    const s = setup()
    const d = await decisions(s)
    expect(d).toHaveLength(1)
    expect(d[0].question).toBe('Q-004 outgrew its limit: make it a ticket, or allow 3 more files?')
    s.store.setViewer('p_tom')
    expect(await decisions(s)).toEqual([])
  })
  it('make it a ticket converts the task and the decision disappears', async () => {
    const s = setup()
    const d = (await decisions(s))[0]
    expect(d.options.map((o) => o.key)).toEqual(['ticket', 'more'])
    const r = await run(s, d.action, { id: d.id, option: 'ticket' })
    expect(r.changed).toBe(true)
    expect((await state(s)).items.find((q) => q.id === 'Q-004')!.status).toBe('converted')
    expect(await decisions(s)).toEqual([])
  })
  it('allow 3 more files reopens the task with a higher limit and the decision disappears', async () => {
    const s = setup()
    const d = (await decisions(s))[0]
    await run(s, d.action, { id: d.id, option: 'more' })
    const q = (await state(s)).items.find((x) => x.id === 'Q-004')!
    expect(q.status).toBe('open')
    expect(await decisions(s)).toEqual([])
  })
  it('deciding twice is refused the second time', async () => {
    const s = setup()
    const d = (await decisions(s))[0]
    await run(s, d.action, { id: d.id, option: 'ticket' })
    const again = await run(s, d.action, { id: d.id, option: 'ticket' })
    expect(again).toMatchObject({ ok: true, message: 'That decision is closed.' })
    expect(again.changed).toBeFalsy()
  })
  it('Make a ticket on the outgrew task removes its decision too', async () => {
    const s = setup()
    await run(s, 'make_ticket', { id: 'Q-004' })
    expect(await decisions(s)).toEqual([])
  })
  it('a maintainer decides, a plain member cannot', async () => {
    const s = setup('p_mara')
    const d = (await decisions(s))[0]
    expect((await run(s, d.action, { id: d.id, option: 'more' })).changed).toBe(true)
  })
})

describe('quick tasks manifest', () => {
  it('declares the settings form with "Agents may add quick tasks" and the action minimum roles', () => {
    const pkg = setup().store.addons.find((a) => a.name === 'quick')!
    const settings = pkg.contributions.find((c) => c.slot === 'settings')!
    expect(JSON.stringify(settings.node)).toContain('Agents may add quick tasks')
    expect(pkg.actions).toMatchObject({ save_settings: { minRole: 'owner' }, decide: { minRole: 'maintainer' } })
  })
})
