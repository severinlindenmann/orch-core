import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { clearPersisted, STORAGE_KEY } from '../persist'
import { createMockStore } from '../store'

// Switching between the normal demo and the busy day: it resets, it persists, it has a route.
beforeEach(() => {
  localStorage.clear()
})
afterEach(() => {
  vi.useRealTimers()
  localStorage.clear()
})

describe('dataset switch', () => {
  it('starts normal and switches with reset(dataset); reset() keeps the mode', () => {
    const s = createMockStore({ persist: false })
    const normal = s.ticketKeys(s.workspaces[0].id).length
    expect(s.dataset).toBe('normal')
    s.reset('busy')
    expect(s.dataset).toBe('busy')
    expect(s.ticketKeys(s.workspaces[0].id).length).toBeGreaterThan(normal + 100)
    s.reset()
    expect(s.dataset).toBe('busy')
    s.reset('normal')
    expect(s.dataset).toBe('normal')
    expect(s.ticketKeys(s.workspaces[0].id).length).toBe(normal)
  })

  it('switching discards the demo changes', () => {
    const s = createMockStore({ persist: false })
    s.append('DEMO-0043', { type: 'log.added', actor: 'p_sev', text: 'a note' })
    s.reset('busy')
    expect(s.eventsOf('DEMO-0043').some((e) => e.text === 'a note')).toBe(false)
  })

  it('persists the mode: a new store (a reload) opens in it', () => {
    const a = createMockStore()
    a.reset('busy')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).dataset).toBe('busy')
    const b = createMockStore()
    expect(b.dataset).toBe('busy')
    b.append('DEMO-0043', { type: 'log.added', actor: 'p_sev', text: 'kept' })
    expect(createMockStore().eventsOf('DEMO-0043').some((e) => e.text === 'kept')).toBe(true)
    createMockStore().reset('normal')
    expect(createMockStore().dataset).toBe('normal')
  })

  it('treats storage without a dataset as normal', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ v: 2, ticketEvents: {}, created: {}, wsEvents: {}, addonState: {} }))
    expect(createMockStore().dataset).toBe('normal')
    clearPersisted()
  })

  it('the options override: dataset busy without persistence', () => {
    expect(createMockStore({ persist: false, dataset: 'busy' }).dataset).toBe('busy')
  })
})

describe('dev routes', () => {
  const make = () => {
    const store = createMockStore({ persist: false })
    const t = createMockTransport(store, { latency: false })
    return { store, t }
  }
  it('GET /api/dev/dataset reports the mode and POST /api/dev/reset switches it', async () => {
    const { store, t } = make()
    expect((await t.request('GET', '/api/dev/dataset')).json).toEqual({ dataset: 'normal' })
    const r = await t.request('POST', '/api/dev/reset', { dataset: 'busy' })
    expect(r.status).toBe(200)
    expect(store.dataset).toBe('busy')
    expect((await t.request('GET', '/api/dev/dataset')).json).toEqual({ dataset: 'busy' })
    await t.request('POST', '/api/dev/reset')
    expect(store.dataset).toBe('busy')
  })
  it('refuses an unknown dataset', async () => {
    const { t, store } = make()
    expect((await t.request('POST', '/api/dev/reset', { dataset: 'huge' })).status).toBe(400)
    expect(store.dataset).toBe('normal')
  })
  it('the client has getDataset and resetDemo(dataset)', async () => {
    const { t } = make()
    const api = createApi(t)
    await api.resetDemo('busy')
    expect(await api.getDataset()).toEqual({ dataset: 'busy' })
  })
})

describe('live background script', () => {
  const live = () => createMockStore({ persist: false, dataset: 'busy', live: true })
  const total = (s: ReturnType<typeof live>) => s.workspaces.flatMap((w) => s.ticketKeys(w.id)).reduce((n, k) => n + s.eventsOf(k).length, 0)

  it('adds one meaningful event every 4 to 8 seconds on the busy day', () => {
    vi.useFakeTimers()
    const s = live()
    const before = total(s)
    vi.advanceTimersByTime(3900)
    expect(total(s)).toBe(before)
    vi.advanceTimersByTime(60_000)
    const added = total(s) - before
    expect(added).toBeGreaterThanOrEqual(7)
    expect(added).toBeLessThanOrEqual(16)
    s.sim.stopAll()
  })

  it('plays task done, comments, questions and claims, as agents and people', () => {
    vi.useFakeTimers()
    const s = live()
    const keys = s.ticketKeys(s.workspaces.find((w) => w.prefix === 'DEMO')!.id)
    const seen = (k: string) => s.eventsOf(k).length
    const before = new Map(keys.map((k) => [k, seen(k)]))
    vi.advanceTimersByTime(30 * 60_000)
    const types = new Set<string>()
    for (const k of keys) for (const e of s.eventsOf(k).slice(before.get(k)!)) types.add(e.type)
    for (const t of ['task.done', 'log.added', 'question.asked', 'claim.taken']) expect(types.has(t), t).toBe(true)
    s.sim.stopAll()
  })

  it('writes events only to tickets that exist, never to restricted ones, with valid actors', () => {
    vi.useFakeTimers()
    const s = live()
    const keys = s.workspaces.flatMap((w) => s.ticketKeys(w.id))
    const before = new Map(keys.map((k) => [k, s.eventsOf(k).length]))
    vi.advanceTimersByTime(20 * 60_000)
    for (const k of keys) {
      const added = s.eventsOf(k).slice(before.get(k)!)
      if (added.length) expect(s.ticket(k)!.restricted, k).toBe(false)
      for (const e of added) expect(['person', 'agent', 'host']).toContain(e.actor.kind)
    }
    s.sim.stopAll()
  })

  it('is capped at 300 events per hour', () => {
    vi.useFakeTimers()
    const s = live()
    const before = total(s)
    vi.advanceTimersByTime(55 * 60_000)
    const added = total(s) - before
    expect(added).toBeGreaterThan(250)
    expect(added).toBeLessThanOrEqual(300)
    s.sim.stopAll()
  })

  it('stops on reset to normal and never runs on the normal day or without live', () => {
    vi.useFakeTimers()
    const s = live()
    expect(s.sim.running()).toContain('busy:live')
    s.reset('normal')
    expect(s.sim.running()).not.toContain('busy:live')
    const before = total(s)
    vi.advanceTimersByTime(120_000)
    expect(total(s)).toBe(before)
    const off = createMockStore({ persist: false, dataset: 'busy' })
    expect(off.sim.running()).toEqual([])
    s.reset('busy')
    expect(s.sim.running()).toContain('busy:live')
    s.sim.stopAll()
  })

  it('does not run in the test app store (no live option there)', async () => {
    const { mockStore } = await import('@/api/client')
    expect(mockStore.sim.running()).not.toContain('busy:live')
  })
})
