import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from './store'

function setup() {
  const store = createMockStore({ persist: false })
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, api }
}

const ids = (items: { ticket: string; kind: string; ref?: string }[]) => items.map((i) => `${i.kind}:${i.ticket}:${i.ref}`)

describe('mock router: viewer-aware views and addon actions', () => {
  it('needs_you differs for Severin, Mara and Tom', async () => {
    const { api, store } = setup()
    const ws = store.workspaces[0].id
    const sev = await api.getToday(ws)
    store.setViewer('p_mara')
    const mara = await api.getToday(ws)
    store.setViewer('p_tom')
    const tom = await api.getToday(ws)
    expect(ids(sev.needs_you)).toContain('question:DEMO-0043:Q2')
    expect(ids(mara.needs_you)).not.toEqual(ids(sev.needs_you))
    expect(ids(mara.needs_you)).not.toContain('question:DEMO-0043:Q2')
    expect(tom.needs_you).toEqual([])
    expect(ids(tom.read_only_open)).toContain('question:DEMO-0043:Q2')
    expect(sev.read_only_open).toEqual([])
    sev.needs_you.filter((i) => i.kind === 'approval').forEach((i) => expect(i.hash).toMatch(/^sha256:/))
  })

  it('shows addon decisions only to owners and maintainers', async () => {
    const { api, store } = setup()
    expect((await api.getAddonDecisions(store.workspaces[0].id)).length).toBeGreaterThan(0)
    store.setViewer('p_tom')
    expect(await api.getAddonDecisions(store.workspaces[0].id)).toEqual([])
  })

  it('github import creates a backlog ticket, removes the issue and records an addon event', async () => {
    const { api, store } = setup()
    const lane = () =>
      JSON.stringify(store.addons.find((a) => a.name === 'github')?.contributions.find((c) => c.slot === 'board.lane')?.node)
    const item = { title: 'Seed loader fails on BOM files', subtitle: 'acme/energy-dbt#118', badge: 'bug' }
    const res = await api.runAddonAction(store.workspaces[0].id, 'github', 'import', { item })
    expect(res).toMatchObject({ ok: true, message: 'Imported GH-118 as DEMO-0050', changed: true })
    const t = await api.getTicket('DEMO-0050')
    expect(t).toMatchObject({ status: 'backlog', type: 'bug', title: item.title })
    expect(t.links.external[0].label).toBe('GH-118')
    expect(store.eventsOf('DEMO-0050').some((e) => e.actor.kind === 'addon' && e.actor.id === 'github')).toBe(true)
    expect(lane()).not.toContain('#118')
    const today = await api.getToday(store.workspaces[0].id)
    expect(today.recent.some((r) => r.ticket === 'DEMO-0050' && r.actor.kind === 'addon')).toBe(true)
  })

  it('refuses done through set_status and allows testing back to in-progress', async () => {
    const { api, store } = setup()
    await expect(api.postAction('DEMO-0043', { action: 'set_status', status: 'done' })).rejects.toMatchObject({
      code: 'human_only',
      message: 'Done is reached by a verdict',
    })
    expect(store.ticket('DEMO-0043')?.status).toBe('in-progress')
    store.append('DEMO-0043', { type: 'status.changed', actor: 'host', to: 'testing' })
    const res = await api.postAction('DEMO-0043', { action: 'set_status', status: 'in-progress' })
    expect(res.ticket.status).toBe('in-progress')
  })
})
