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
