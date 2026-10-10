import { describe, expect, it } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { ApiError } from '@/api/types'
import { reloginItems } from '@/api/attention'
import { parseActor } from './derive'
import { createMockStore, sessionBelongsTo } from './store'

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
    expect(res.event?.type).toBe('question.answered')
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

describe('add_label', () => {
  it('adding a label the ticket already has is a no-op: no event, the ticket back', async () => {
    const { api, store } = setup()
    const label = store.ticket('DEMO-0043')!.labels[0]
    expect(label).toBeTruthy()
    const before = store.eventsOf('DEMO-0043').length
    const res = await api.postAction('DEMO-0043', { action: 'add_label', label: label.toUpperCase() })
    expect(res.event).toBeNull()
    expect(res.ticket.labels).toContain(label)
    expect(store.eventsOf('DEMO-0043')).toHaveLength(before)
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
    expect(res.event?.type).toBe('labels.changed')
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
    it('revoke also ends the lease of a subagent the grant does not list, but not a look-alike session', async () => {
      const { api, store } = setup()
      const ws = store.workspaces[0].id
      store.append('DEMO-0043', { type: 'lease.taken', actor: 'claude-code:s_77c2.3:p_sev', task: 'T4' })
      expect((await api.getTicket('DEMO-0043')).tasks_state.find((t) => t.id === 'T4')?.lease?.session).toBe('s_77c2.3')
      await api.revokeGrant(ws, 'gr_01J9Z8')
      expect((await api.getTicket('DEMO-0043')).tasks_state.find((t) => t.id === 'T4')?.lease).toBeNull()
      expect(sessionBelongsTo('s_77c2.3', 's_77c2')).toBe(true)
      expect(sessionBelongsTo('s_77c21', 's_77c2')).toBe(false)
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
    it('a member issues a grant for themselves: the tickets they may work on, up to the workspace default; revokes their own, not others', async () => {
      const { api, store } = setup()
      const cli = store.workspaces.find((w) => w.prefix === 'CLI')!.id
      expect(store.roleIn(cli, 'p_tom')).toBe('member')
      store.appendWs(cli, { type: 'grant.issued', actor: 'p_sev', grant: 'gr_sev', person: 'p_sev', scope: 'all', until: '2026-10-09T18:00:00Z', hours: 4, sessions: [] })
      store.setViewer('p_tom')
      // Not all tickets, and not longer than the workspace default (8 h).
      await expect(api.issueGrant(cli, { hours: 4, scope: 'all' })).rejects.toMatchObject({ status: 403, code: 'grant.scope' })
      await expect(api.issueGrant(cli, { hours: 9, scope: 'workable' })).rejects.toMatchObject({ status: 400, code: 'validation' })
      const g = await api.issueGrant(cli, { hours: 8, scope: 'workable' })
      expect(g).toMatchObject({ person: 'p_tom', scope: 'workable', revoked: null })
      expect(store.wsEventsOf(cli).at(-1)).toMatchObject({ type: 'grant.issued', presence: 'touchid', actor: { kind: 'person', id: 'p_tom' } })
      await expect(api.revokeGrant(cli, 'gr_sev')).rejects.toMatchObject({ status: 403, code: 'forbidden' })
      expect((await api.revokeGrant(cli, g.id)).revoked?.by).toBe('p_tom')
    })
    it('an owner revokes a member grant; a viewer cannot issue one', async () => {
      const { api, store } = setup()
      const cli = store.workspaces.find((w) => w.prefix === 'CLI')!.id
      store.setViewer('p_tom')
      const g = await api.issueGrant(cli, { hours: 2, scope: 'workable' })
      store.setViewer('p_sev')
      expect((await api.revokeGrant(cli, g.id)).revoked?.by).toBe('p_sev')
      const demo = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
      store.setViewer('p_tom')
      expect(store.roleIn(demo, 'p_tom')).toBe('viewer')
      await expect(api.issueGrant(demo, { hours: 2, scope: 'workable' })).rejects.toMatchObject({ status: 403, code: 'forbidden' })
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

describe('addon action route', () => {
  it('404s for an unknown workspace and creates no state', async () => {
    const { api, store } = setup()
    await expect(api.runAddonAction('bogus', 'estimate', 'save_settings', { formData: {} })).rejects.toMatchObject({ status: 404 })
    const states = (store as unknown as { addonStates: Record<string, unknown> }).addonStates
    expect(Object.keys(states).some((k) => k.startsWith('bogus'))).toBe(false)
  })
  it('is scoped to a workspace: the old global route is gone', async () => {
    const { store } = setup()
    const transport = createMockTransport(store, { latency: false })
    expect((await transport.request('POST', '/api/addons/estimate/actions/set', { ws: store.workspaces[0].id })).status).toBe(404)
    const ws = store.workspaces[0].id
    expect((await transport.request('POST', `/api/workspaces/${ws}/addons/github/actions/refresh`, {})).status).toBe(200)
  })
  it('refuses a ticket from another workspace with 409 ticket.other_workspace', async () => {
    const { api, store } = setup()
    const demo = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
    const before = store.eventsOf('INT-0007').length
    await expect(api.runAddonAction(demo, 'estimate', 'set', { ticket: 'INT-0007', formData: { points: 3 } })).rejects.toMatchObject({ status: 409, code: 'ticket.other_workspace' })
    expect(store.eventsOf('INT-0007')).toHaveLength(before)
    expect((await api.runAddonAction(demo, 'estimate', 'set', { ticket: 'DEMO-0043', formData: { points: 3 } })).ok).toBe(true)
  })
  describe('roles', () => {
    const run = (api: ReturnType<typeof setup>['api'], ws: string, addon: string, action: string, body: Record<string, unknown> = {}) =>
      api.runAddonAction(ws, addon, action, body).then(
        () => 'ok',
        (e: ApiError) => `${e.status} ${e.code}`,
      )
    it('refuses every action to a viewer (403)', async () => {
      const { api, store } = setup()
      const demo = store.workspaces[0].id
      store.setViewer('p_tom')
      const before = store.eventsOf('DEMO-0043').length
      expect(await run(api, demo, 'estimate', 'set', { ticket: 'DEMO-0043', formData: { points: 13 } })).toBe('403 forbidden')
      expect(await run(api, demo, 'publish', 'share', { ticket: 'DEMO-0043' })).toBe('403 forbidden')
      expect(await run(api, demo, 'github', 'refresh')).toBe('403 forbidden')
      expect(store.ticket('DEMO-0043')!.addons.estimate?.points).not.toBe(13)
      expect(store.eventsOf('DEMO-0043')).toHaveLength(before)
    })
    it('lets a member run member actions (Tom is a member in CLI)', async () => {
      const { api, store } = setup()
      const cli = store.workspaces.find((w) => w.prefix === 'CLI')!.id
      store.setViewer('p_tom')
      expect(await run(api, cli, 'estimate', 'set', { ticket: 'CLI-0003', formData: { points: 2 } })).toBe('ok')
    })
    it('honours a per-action minimum role: save_settings owner, decide maintainer', async () => {
      const { api, store } = setup()
      const demo = store.workspaces[0].id
      store.appendWs(demo, { type: 'member.added', person: 'p_mem', name: 'Mem', role: 'member' })
      store.setViewer('p_mara')
      expect(await run(api, demo, 'estimate', 'save_settings', { formData: {} })).toBe('403 forbidden')
      expect(await run(api, demo, 'publish', 'decide', { confirmed: true, option: 'no', id: 'nope' })).toBe('409 decision.closed') // allowed to decide; that one is not open
      store.setViewer('p_mem')
      expect(await run(api, demo, 'publish', 'decide', { confirmed: true, option: 'no', id: 'nope' })).toBe('403 forbidden')
      expect(await run(api, demo, 'publish', 'share', { ticket: 'DEMO-0043' })).toBe('ok')
      store.setViewer('p_sev')
      expect(await run(api, demo, 'estimate', 'save_settings', { formData: {} })).toBe('ok')
    })
  })
})

describe('stable ages and the attention count', () => {
  it('an item keeps its age when another item is decided', async () => {
    const { api, store } = setup()
    const ws = store.workspaces[0].id
    const before = (await api.getToday(ws)).needs_you
    const answered = before.find((i) => i.kind === 'question')!
    await api.postAction(answered.ticket, { action: 'answer', question: answered.ref!, option: 'date' }).catch(() => undefined)
    store.append(answered.ticket, { type: 'comment.added', actor: 'p_sev', text: 'noted' })
    const after = (await api.getToday(ws)).needs_you
    const key = (i: { kind: string; ticket: string; ref?: string }) => `${i.kind}:${i.ticket}:${i.ref}`
    for (const i of after) expect(i.since, key(i)).toBe(before.find((b) => key(b) === key(i))!.since)
    expect(after.length).toBeLessThan(before.length)
  })

  it('a verdict item is as old as the move to Testing, not the last event', () => {
    const store = createMockStore({ persist: false, dataset: 'busy' })
    const ws = store.workspaces[0].id
    const v = store.needsYou(ws).find((i) => i.kind === 'verdict')!
    const moved = store.eventsOf(v.ticket).filter((e) => e.type === 'status.changed' && e.to === 'testing').at(-1)!.at
    expect(v.since).toBe(moved)
    store.append(v.ticket, { type: 'comment.added', actor: 'p_sev', text: 'later' })
    expect(store.needsYou(ws).find((i) => i.kind === 'verdict' && i.ticket === v.ticket)!.since).toBe(moved)
  })

  it('workspace needs_you counts open addon decisions the viewer can decide, and the owner\'s re-logins (R-c)', async () => {
    const { api, store } = setup()
    const ws = store.workspaces[0].id
    const core = (await api.getToday(ws)).needs_you.length
    const addon = (await api.getAddonDecisions(ws)).length
    expect(addon).toBeGreaterThan(0)
    const relogin = reloginItems(await api.getConnections(ws)).length
    expect(relogin).toBeGreaterThan(0)
    expect((await api.getWorkspaces()).find((w) => w.id === ws)!.needs_you).toBe(core + addon + relogin)
    store.setViewer('p_tom')
    expect((await api.getWorkspaces()).find((w) => w.id === ws)!.needs_you).toBe(0)
  })

  it('Today says what waits on other people, and who', async () => {
    const { api, store } = setup()
    const ws = store.workspaces[0].id
    const today = await api.getToday(ws)
    expect(today.waiting_on_others.count).toBeGreaterThan(0)
    expect(today.waiting_on_others.people).toContain('p_mara')
    expect(today.waiting_on_others.people).not.toContain('p_sev')
  })

  it('resetting or switching dataset keeps the viewer when asked', () => {
    const { store } = setup()
    store.setViewer('p_mara')
    store.reset('busy', true)
    expect(store.viewer).toBe('p_mara')
    store.reset('normal')
    expect(store.viewer).toBe('p_sev')
  })
})

describe('grant default hours', () => {
  it('is clamped to 1..12 whole hours', async () => {
    const { grantDefaultHours } = await import('@/api/grants')
    expect(grantDefaultHours({ grant_hours: 99 })).toBe(12)
    expect(grantDefaultHours({ grant_hours: 0 })).toBe(1)
    expect(grantDefaultHours({ grant_hours: Number.NaN })).toBe(8)
    expect(grantDefaultHours({})).toBe(8)
  })
})
