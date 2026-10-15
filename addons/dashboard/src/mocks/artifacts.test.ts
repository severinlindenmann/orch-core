import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore } from './store'

const setup = (viewer = 'p_sev', dataset: 'normal' | 'busy' = 'normal') => {
  const store = createMockStore({ persist: false, dataset })
  store.setViewer(viewer)
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  return { store, ws, api: createApi(createMockTransport(store, { latency: false })) }
}
const fail = (p: Promise<unknown>) => p.then(() => 'ok', (e: { status: number; code: string }) => `${e.status} ${e.code}`)

describe('GET /api/workspaces/:ws/artifacts', () => {
  it('lists every artifact of the workspace newest first, with its ticket and who added it, and no inline content', async () => {
    const { api, ws } = setup()
    const page = await api.listArtifacts(ws)
    expect(page.total).toBe(12)
    expect(page.items.map((i) => i.at)).toEqual([...page.items.map((i) => i.at)].sort().reverse())
    const log = page.items.find((i) => i.name === 'tariff-export.log')!
    expect(log).toMatchObject({ ticket: 'DEMO-0043', kind: 'log', by: { kind: 'agent', id: 'claude-code', for: 'p_sev' }, has_preview: true })
    expect(log).not.toHaveProperty('preview')
    expect(page.items.find((i) => i.name === 'share/demo-0043-preview')!.by).toEqual({ kind: 'addon', id: 'publish' })
    expect(page.items.find((i) => i.name === 't2-check-s_a41.log')!.by).toMatchObject({ kind: 'agent', id: 'codex', for: 'p_mara' })
  })
  it('enforces visibility on the host: a restricted ticket\'s artifacts are listed only for the people it names', async () => {
    const { store, api, ws } = setup('p_tom')
    store.append('DEMO-0044', { type: 'artifact.added', actor: 'p_sev', name: 'incident-notes.md', kind: 'report', bytes: 900, sha256: 'a'.repeat(64) })
    const tom = await api.listArtifacts(ws)
    expect(tom.items.some((i) => i.ticket === 'DEMO-0044')).toBe(false)
    expect(tom.facets.tickets.some((t) => t.key === 'DEMO-0044')).toBe(false)
    expect((await api.listArtifacts(ws, { ticket: 'DEMO-0044' })).total).toBe(0)
    store.setViewer('p_mara')
    expect((await api.listArtifacts(ws, { ticket: 'DEMO-0044' })).items.map((i) => i.name)).toEqual(['incident-notes.md'])
  })
  it('is for members only', async () => {
    const { store, api } = setup('p_tom')
    const int = store.workspaces.find((w) => w.prefix === 'INT')!.id
    expect(await fail(api.listArtifacts(int))).toBe('403 forbidden')
    expect(await fail(api.listArtifacts('nope'))).toBe('404 not_found')
  })
  it('filters by kind, ticket, adder (person, agent, agents, people), date and text', async () => {
    const { api, ws } = setup()
    expect((await api.listArtifacts(ws, { kind: 'log' })).items.every((i) => i.kind === 'log')).toBe(true)
    expect((await api.listArtifacts(ws, { kind: 'log' })).total).toBe(4)
    expect((await api.listArtifacts(ws, { ticket: 'DEMO-0041' })).total).toBe(4)
    expect((await api.listArtifacts(ws, { by: 'codex' })).items.map((i) => i.name)).toEqual(['t2-check-s_a41.log'])
    expect((await api.listArtifacts(ws, { by: 'agents' })).total).toBe(11)
    expect((await api.listArtifacts(ws, { by: 'people' })).total).toBe(0)
    // Mock now is 9 Oct 11:30: the last 24 h holds DEMO-0043's and Mara's check.
    const day = await api.listArtifacts(ws, { since: '24h' })
    expect(day.items.every((i) => i.at >= '2026-10-08T11:30:00Z')).toBe(true)
    expect(day.total).toBe(8)
    expect((await api.listArtifacts(ws, { q: 'tolerance' })).items.map((i) => i.name)).toEqual(['tolerance-demo.html'])
  })
  it('counts facets over the other filters, so each menu says what it would find', async () => {
    const { api, ws } = setup()
    const p = await api.listArtifacts(ws, { kind: 'log' })
    expect(p.facets.kinds.find((k) => k.kind === 'screenshot')!.count).toBe(2) // kind facet ignores the kind filter
    expect(p.facets.tickets.map((t) => [t.key, t.count])).toEqual([['DEMO-0041', 1], ['DEMO-0043', 1], ['DEMO-0045', 2]].sort())
  })
  it('pages on the host (busy day): 48 per page by default, bounded page numbers', async () => {
    const { api, ws } = setup('p_sev', 'busy')
    const first = await api.listArtifacts(ws)
    expect(first.total).toBeGreaterThan(100)
    expect(first.items).toHaveLength(48)
    expect(first.pages).toBe(Math.ceil(first.total / 48))
    const last = await api.listArtifacts(ws, { page: 9999 })
    expect(last.page).toBe(first.pages)
    expect(last.items.length).toBe(first.total - (first.pages - 1) * 48)
    expect((await api.listArtifacts(ws, { per: 100000 })).per).toBe(200)
    expect(JSON.stringify(first).length).toBeLessThan(80_000) // no previews ride along
  })
})
