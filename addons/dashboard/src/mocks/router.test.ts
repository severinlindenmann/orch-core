import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { parseActor } from './derive'
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

  describe('grants', () => {
    it('revoking releases the claim on DEMO-0043 and ends the leases', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      expect((await api.getTicket('DEMO-0043')).claim).not.toBeNull()
      const g = await api.revokeGrant(ws, 'gr_01J9Z8')
      expect(g.revoked?.by).toBe('p_sev')
      const t = await api.getTicket('DEMO-0043')
      expect(t.claim).toBeNull()
      expect(t.tasks_state.every((x) => x.lease === null)).toBe(true)
      const released = store.eventsOf('DEMO-0043').filter((e) => e.type === 'claim.released').pop()
      expect(released).toMatchObject({ reason: 'grant revoked' })
      const sessions = await api.getAgents(ws)
      expect(sessions.filter((s) => s.grant?.id === 'gr_01J9Z8').every((s) => s.state === 'stopped')).toBe(true)
      await expect(api.revokeGrant(ws, 'gr_01J9Z8')).rejects.toMatchObject({ status: 409 })
    })
    it('Mara cannot revoke Severin\'s grant, a viewer cannot revoke or issue', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      store.setViewer('p_mara')
      await expect(api.revokeGrant(ws, 'gr_01J9Z8')).rejects.toMatchObject({ status: 403 })
      store.setViewer('p_tom')
      await expect(api.revokeGrant(ws, 'gr_01J9Y4')).rejects.toMatchObject({ status: 403 })
      await expect(api.issueGrant(ws, { hours: 4, scope: 'all' })).rejects.toMatchObject({ status: 403 })
      expect((await api.listGrants(ws)).find((g) => g.id === 'gr_01J9Z8')?.revoked).toBeNull()
    })
    it('the owner may revoke Mara\'s grant; Mara may revoke her own', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      expect((await api.revokeGrant(ws, 'gr_01J9Y4')).revoked?.by).toBe('p_sev')
      const other = setup()
      other.store.setViewer('p_mara')
      expect((await other.api.revokeGrant(other.store.workspaces[0].id, 'gr_01J9Y4')).revoked?.by).toBe('p_mara')
    })
    it('issues a grant for the viewer and validates the hours', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      await expect(api.issueGrant(ws, { hours: 13, scope: 'all' })).rejects.toMatchObject({ status: 400 })
      await expect(api.issueGrant(ws, { hours: 0, scope: 'all' })).rejects.toMatchObject({ status: 400 })
      const g = await api.issueGrant(ws, { hours: 4, scope: 'all' })
      expect(g).toMatchObject({ person: 'p_sev', scope: 'all', revoked: null })
      expect(Date.parse(g.until) - Date.parse(g.issued_at)).toBe(4 * 3600_000)
      expect(store.wsEventsOf(ws).some((e) => e.type === 'grant.issued' && e.grant === g.id)).toBe(true)
    })
    it('an agent actor cannot issue or revoke a grant (human_only)', () => {
      const store = createMockStore({ persist: false })
      const ws = store.workspaces[0].id
      const agent = parseActor('claude-code:s_77c2:p_sev')
      expect(store.issueGrant(ws, { hours: 4, scope: 'all' }, agent)).toMatchObject({ ok: false, status: 403, code: 'human_only' })
      expect(store.revokeGrant(ws, 'gr_01J9Z8', agent)).toMatchObject({ ok: false, status: 403, code: 'human_only' })
      expect(store.wsEventsOf(ws).some((e) => e.type === 'grant.issued' || e.type === 'grant.revoked')).toBe(false)
    })
  })

  describe('agents', () => {
    it('lists sessions with subagents, models and states', async () => {
      const { api, store } = setup()
      const list = await api.getAgents(store.workspaces[0].id)
      const sub = list.find((s) => s.session === 's_77c2.1')!
      expect(sub).toMatchObject({ parent: 's_77c2', harness: 'claude-code', state: 'waiting' })
      expect(sub.waiting_on).toMatchObject({ kind: 'question', ticket: 'DEMO-0043' })
      expect(list.filter((s) => s.state === 'working')).toHaveLength(3)
    })
    it('activity: newest first, refusals carry the stop rule on the third same code', async () => {
      const { api, store } = setup()
      const feed = await api.getAgentActivity(store.workspaces[0].id)
      expect(feed.length).toBeGreaterThanOrEqual(25)
      expect(feed.length).toBeLessThanOrEqual(50)
      expect([...feed].sort((a, b) => b.at.localeCompare(a.at))).toEqual(feed)
      const refusals = feed.filter((f) => f.refusal)
      expect(refusals.map((r) => r.refusal!.code).sort()).toEqual(['claim.held', 'human_only', 'lease.held', 'lease.held', 'lease.held'])
      expect(refusals.filter((r) => r.refusal!.stop)).toHaveLength(1)
      expect(refusals.find((r) => r.refusal!.stop)!.refusal!.code).toBe('lease.held')
    })
  })
})
