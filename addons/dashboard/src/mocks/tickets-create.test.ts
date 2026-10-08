import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { createMockStore, type MockStore } from '@/mocks/store'
import type { NewTicketRequest } from '@/api/types'

const apiOf = (store: MockStore) => createApi(createMockTransport(store, { latency: false }))
const setup = () => {
  const store = createMockStore({ persist: false })
  return { store, api: apiOf(store), ws: store.workspaces[0].id }
}
const base: NewTicketRequest = {
  type: 'feature',
  title: 'A new ticket',
  priority: 'medium',
  size: null,
  labels: [],
  parent: null,
  due: null,
  visibility: 'workspace',
  people: { owner: null, assignees: [], reviewers: [] },
  sections: {},
  acceptance: [],
}

describe('create ticket', () => {
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
    const { api, store, ws } = setup()
    store.setViewer('p_tom')
    await expect(api.createTicket(ws, { ...base, sections: { requirements: 'x' } })).rejects.toMatchObject({ status: 403 })
  })
  it('a created ticket survives a reload', async () => {
    const a = createMockStore({ persist: true })
    const api = apiOf(a)
    const { ticket } = await api.createTicket(a.workspaces[0].id, { ...base, sections: { requirements: 'x' } })
    expect(createMockStore({ persist: true }).ticket(ticket.key)?.title).toBe(base.title)
  })
  it('lists the ticket, numbers acceptance criteria and starts with ticket.created', async () => {
    const { api, store, ws } = setup()
    const before = store.cursor(ws)
    const { ticket } = await api.createTicket(ws, { ...base, sections: { requirements: 'x' }, acceptance: ['It works', 'It is fast'], people: { owner: 'p_mara', assignees: [], reviewers: [] } })
    expect((await api.listTickets(ws)).some((t) => t.key === ticket.key)).toBe(true)
    expect(ticket.acceptance.map((a) => a.id)).toEqual(['AC1', 'AC2'])
    expect(ticket.people.owner).toBe('p_mara')
    expect((await api.getEvents(ticket.key))[0].type).toBe('ticket.created')
    expect(store.cursor(ws)).toBeGreaterThan(before)
  })
  it('validates title length and size', async () => {
    const { api, ws } = setup()
    await expect(api.createTicket(ws, { ...base, title: 'ab', sections: { requirements: 'x' } })).rejects.toMatchObject({ code: 'validation.title' })
    await expect(api.createTicket(ws, { ...base, size: 'huge' as never, sections: { requirements: 'x' } })).rejects.toMatchObject({ code: 'validation.size' })
  })
})
