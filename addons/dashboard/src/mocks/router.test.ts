import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { createMockStore } from './store'

function setup() {
  const store = createMockStore({ persist: false })
  const api = createApi(createMockTransport(store, { latency: false }))
  return { store, api }
}

describe('mock router', () => {
  it('returns DEMO-0043 as a derived ticket document', async () => {
    const { api } = setup()
    const t = await api.getTicket('DEMO-0043')
    expect(t.title).toBe('Load tariff tables as dbt seeds')
    expect(t.status).toBe('in-progress')
    expect(t.tasks_state.map((x) => x.state)).toEqual(['done', 'doing', 'doing', 'todo'])
    expect(t.questions_state.find((q) => q.id === 'Q1')?.state).toBe('answered')
    expect(t.questions_state.find((q) => q.id === 'Q2')?.state).toBe('open')
    expect(t.claim?.agent).toBe('claude-code')
  })

  it('answering Q2 changes the store and appends an event', async () => {
    const { api, store } = setup()
    const before = store.eventsOf('DEMO-0043').length
    const res = await api.postAction('DEMO-0043', { action: 'answer', question: 'Q2', option: 'date' })
    expect(res.event.type).toBe('question.answered')
    expect(store.eventsOf('DEMO-0043')).toHaveLength(before + 1)
    expect(res.ticket.questions_state.find((q) => q.id === 'Q2')?.answer?.option).toBe('date')
    const today = await api.getToday(store.workspaces[0].id)
    expect(today.needs_you.some((n) => n.ticket === 'DEMO-0043' && n.ref === 'Q2')).toBe(false)
    await expect(api.postAction('DEMO-0043', { action: 'answer', question: 'Q2', option: 'date' })).rejects.toMatchObject({
      code: 'question.already_answered',
    })
  })

  it('hides the restricted ticket from a viewer', async () => {
    const { api, store } = setup()
    expect((await api.getTicket('DEMO-0044')).restricted).toBe(true)
    store.setViewer('p_tom')
    await expect(api.getTicket('DEMO-0044')).rejects.toBeInstanceOf(ApiError)
    const list = await api.listTickets(store.workspaces[0].id)
    expect(list.some((t) => t.key === 'DEMO-0044')).toBe(false)
  })
})

describe('mock tickets search', () => {
  it('q searches body sections and returns a snippet', async () => {
    const { api, store } = setup()
    const ws = store.workspaces[0].id
    const hits = await api.listTickets(ws, { q: 'fct_billing' })
    const t = hits.find((x) => x.key === 'DEMO-0043')
    expect(t?.match?.section).toBeTruthy()
    expect(t!.match!.snippet).toContain('«fct_billing»')
    expect(t!.match!.snippet.length).toBeLessThanOrEqual(140)
  })
  it('sort=priority puts urgent first; filters narrow the list', async () => {
    const { api, store } = setup()
    const ws = store.workspaces[0].id
    const list = await api.listTickets(ws, { sort: 'priority' })
    expect(list[0].priority).toBe('urgent')
    const high = await api.listTickets(ws, { priority: ['urgent'] })
    expect(high.length).toBeGreaterThan(0)
    expect(high.every((x) => x.priority === 'urgent')).toBe(true)
    const mine = await api.listTickets(ws, { needs: 'me' })
    expect(mine.every((x) => x.turn.who === 'p_sev')).toBe(true)
    const restricted = await api.listTickets(ws, { restricted: true })
    expect(restricted.every((x) => x.restricted)).toBe(true)
    const person = await api.listTickets(ws, { person: 'p_mara' })
    expect(person.every((x) => x.owner === 'p_mara' || x.assignees.includes('p_mara') || x.claim?.for === 'p_mara')).toBe(true)
  })
  it('add_label adds a label for owners and is refused for viewers', async () => {
    const { api, store } = setup()
    const res = await api.postAction('DEMO-0043', { action: 'add_label', label: 'q4' })
    expect(res.event.type).toBe('labels.changed')
    expect(res.ticket.labels).toContain('q4')
    store.setViewer('p_tom')
    await expect(api.postAction('DEMO-0043', { action: 'add_label', label: 'x' })).rejects.toMatchObject({ code: 'forbidden' })
  })

  describe('saved views', () => {
    it('lists seeded views by visibility', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      expect((await api.listViews(ws)).map((v) => v.name)).toEqual(['My open work', 'Release blockers'])
      store.setViewer('p_mara')
      expect((await api.listViews(ws)).map((v) => v.name)).toEqual(['Release blockers'])
    })
    it('saves a view as a view.saved event; viewers cannot share', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      const v = await api.saveView(ws, { name: 'Bugs', shared: true, params: { type: 'bug' } })
      expect(v).toMatchObject({ name: 'Bugs', owner: 'p_sev', shared: true })
      expect(store.wsEventsOf(ws).some((e) => e.type === 'view.saved')).toBe(true)
      store.setViewer('p_tom')
      await expect(api.saveView(ws, { name: 'X', shared: true, params: {} })).rejects.toMatchObject({ code: 'forbidden' })
      expect((await api.saveView(ws, { name: 'Mine', shared: false, params: {} })).owner).toBe('p_tom')
    })
    it('only the owner deletes a view', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      const shared = (await api.listViews(ws)).find((v) => v.name === 'Release blockers')!
      store.setViewer('p_mara')
      await expect(api.deleteView(ws, shared.id)).rejects.toMatchObject({ code: 'forbidden' })
      store.setViewer('p_sev')
      await api.deleteView(ws, shared.id)
      expect((await api.listViews(ws)).map((v) => v.name)).toEqual(['My open work'])
      expect(store.wsEventsOf(ws).some((e) => e.type === 'view.deleted')).toBe(true)
    })
  })
})
