import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { describeEvent } from '@/mocks/derive'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'

const setup = (viewer = 'p_sev') => {
  const store = createMockStore({ persist: false })
  const ws = store.workspaces.find((w) => w.prefix === 'DEMO')!.id
  installAndGrant(store, ws, 'activity')
  store.setViewer(viewer)
  return { store, api: createApi(createMockTransport(store, { latency: false })), ws }
}
type S = ReturnType<typeof setup>
interface Row {
  day: string
  at: string
  actor: string
  kind: string
  ticket?: string
  group: string
  count: number
  summary: string
  title: string
  subtitle?: string
}
interface Item {
  title: string
  badge?: string
  status?: string
  actions?: { label: string; action: string; args?: Record<string, unknown> }[]
}
interface State {
  timeline: Row[]
  total: number
  hidden: number
  hasMore: boolean
  filters: { groups: string[]; people: string[]; agents: string[]; q: string }
  typeFilters: Item[]
  peopleFilters: Item[]
  agentFilters: Item[]
  todaySummary: string
  today: { events: number; byAgents: number }
  ticketRows: { ticket: string; today: number; last_actor: string }[]
  view: { type: string; children?: { type: string; text?: string; items?: { title: string }[] }[] }
}
const state = async (s: S) => (await s.api.getAddonState(s.ws, 'activity')) as unknown as State
const run = (s: S, id: string, body: Record<string, unknown> = {}) => s.api.runAddonAction(s.ws, 'activity', id, body)
const comment = (s: S, key: string, text: string) => s.api.postAction(key, { action: 'comment', text })
const AGENT = 'claude-code:s_x1:p_sev'
// The mock clock has one-second resolution: let a few seconds pass so a later event is later than the install events.
const tick = (sec = 5) => vi.advanceTimersByTime(sec * 1000)
beforeEach(() => vi.useFakeTimers({ toFake: ['Date'] }))
afterEach(() => vi.useRealTimers())

describe('activity timeline', () => {
  it('lists ticket and workspace events, newest first, each with a readable summary', async () => {
    const s = setup()
    tick()
    s.store.appendWs(s.ws, { type: 'view.saved', view: 'v1', name: 'My open work', shared: false, params: {} })
    const st = await state(s)
    expect(st.timeline.length).toBeGreaterThan(10)
    const ats = st.timeline.map((r) => r.at)
    expect([...ats].sort().reverse()).toEqual(ats)
    expect(st.timeline[0]).toMatchObject({ actor: 'Severin', ticket: undefined })
    expect(st.timeline[0].title).toMatch(/^Severin · workspace · /)
    for (const r of st.timeline) {
      expect(r.summary.trim(), r.title).not.toBe('')
      expect(r.title).not.toMatch(/undefined|null|\s$|labelled\s*$/)
    }
    expect(st.timeline.some((r) => r.ticket === 'DEMO-0043')).toBe(true)
  })
  it('groups by day: a day heading per day, newest first', async () => {
    const st = await state(setup())
    const days = [...new Set(st.timeline.map((r) => r.day))]
    expect(days.length).toBeGreaterThan(2)
    expect([...days].sort().reverse()).toEqual(days)
    const heads = st.view.children!.filter((c) => c.type === 'markdown').map((c) => c.text!)
    expect(heads).toHaveLength(days.length)
    expect(heads[0]).toMatch(/^### Today/)
  })
  it('collapses a run by one actor on one ticket into one row with a count', async () => {
    const s = setup()
    tick()
    for (const t of ['T1', 'T2', 'T3', 'T4', 'T5']) s.store.append('DEMO-0044', { type: 'task.done', task: t, actor: AGENT })
    const st = await state(s)
    const top = st.timeline[0]
    expect(top).toMatchObject({ actor: 'claude-code', ticket: 'DEMO-0044', count: 5, group: 'tasks' })
    expect(top.title).toBe('claude-code · DEMO-0044 · 5 task updates')
    expect(st.timeline.filter((r) => r.ticket === 'DEMO-0044' && r.actor === 'claude-code' && r.group === 'tasks')).toHaveLength(1)
  })
  it('does not collapse across different actors or tickets', async () => {
    const s = setup()
    tick()
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T1', actor: AGENT })
    s.store.append('DEMO-0045', { type: 'task.done', task: 'T1', actor: AGENT })
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T2', actor: 'codex:s_x2:p_mara' })
    const top = (await state(s)).timeline.slice(0, 3)
    expect(top.map((r) => r.count)).toEqual([1, 1, 1])
    expect(top[0].title).toBe('codex · DEMO-0044 · finished T2')
  })
  it('caps the default view at 50 rows with Show older, per viewer', async () => {
    const s = setup()
    for (let i = 0; i < 70; i++) {
      s.store.append(i % 2 ? 'DEMO-0044' : 'DEMO-0045', { type: 'log.added', text: `n${i}`, actor: i % 3 ? 'p_sev' : 'p_mara' })
    }
    const st = await state(s)
    expect(st.timeline).toHaveLength(50)
    expect(st.hasMore).toBe(true)
    expect(st.hidden).toBeGreaterThan(0)
    await run(s, 'show_older')
    const more = await state(s)
    expect(more.timeline).toHaveLength(100)
    s.store.setViewer('p_mara')
    expect((await state(s)).timeline).toHaveLength(50)
  })
  it('stops offering Show older once everything is shown', async () => {
    const s = setup()
    for (let i = 0; i < 3; i++) await run(s, 'show_older')
    const st = await state(s)
    expect(st.hasMore).toBe(false)
    expect(st.hidden).toBe(0)
    expect(st.timeline.length).toBe(st.total)
  })
  it('a comment elsewhere appears at the top after the cursor bump', async () => {
    const s = setup()
    const before = await s.api.getCursor(s.ws)
    tick()
    await comment(s, 'DEMO-0043', 'Looks good, shipping')
    expect((await s.api.getCursor(s.ws)).cursor).toBeGreaterThan(before.cursor)
    const top = (await state(s)).timeline[0]
    expect(top).toMatchObject({ ticket: 'DEMO-0043', actor: 'Severin' })
    expect(top.summary).toBe('logged a note')
  })
})

describe('activity filters (per viewer)', () => {
  it('filtering to gates shows only gate events', async () => {
    const s = setup()
    await run(s, 'toggle_group', { group: 'gates' })
    const st = await state(s)
    expect(st.timeline.length).toBeGreaterThan(0)
    expect(st.timeline.every((r) => r.group === 'gates')).toBe(true)
    expect(st.filters.groups).toEqual(['gates'])
    const gates = st.typeFilters.find((i) => i.title.startsWith('Gates'))!
    expect(gates.status).toBe('ok')
    expect(gates.actions![0].label).toBe('Remove filter')
  })
  it('lists the seven type groups, people and agents as list items with counts', async () => {
    const st = await state(setup())
    expect(st.typeFilters.map((i) => i.title.split(' ')[0])).toEqual(['Status', 'Gates', 'Questions', 'Tasks', 'Artifacts', 'Addons', 'Workspace'])
    expect(st.typeFilters.every((i) => /^\d+$/.test(i.badge ?? ''))).toBe(true)
    expect(st.peopleFilters.map((i) => i.title)).toEqual(expect.arrayContaining(['Severin', 'Mara']))
    expect(st.agentFilters.map((i) => i.title)).toEqual(expect.arrayContaining(['claude-code', 'codex']))
  })
  it('filters by person, by agent, and the two combine as "these actors"', async () => {
    const s = setup()
    await run(s, 'toggle_agent', { id: 'codex' })
    let st = await state(s)
    expect(st.timeline.every((r) => r.actor === 'codex')).toBe(true)
    await run(s, 'toggle_person', { id: 'p_mara' })
    st = await state(s)
    expect(new Set(st.timeline.map((r) => r.actor))).toEqual(new Set(['codex', 'Mara']))
    await run(s, 'toggle_agent', { id: 'codex' })
    await run(s, 'toggle_person', { id: 'p_mara' })
    expect((await state(s)).timeline.some((r) => r.actor === 'claude-code')).toBe(true)
  })
  it('search matches summary, actor and ticket; Clear filters resets everything', async () => {
    const s = setup()
    await run(s, 'search', { formData: { q: 'DEMO-0043' } })
    let st = await state(s)
    expect(st.timeline.length).toBeGreaterThan(0)
    expect(st.timeline.every((r) => r.ticket === 'DEMO-0043')).toBe(true)
    await run(s, 'toggle_group', { group: 'gates' })
    await run(s, 'clear_filters')
    st = await state(s)
    expect(st.filters).toEqual({ groups: [], people: [], agents: [], q: '' })
    expect(st.timeline.length).toBeGreaterThan(10)
  })
  it('filters belong to the viewer', async () => {
    const s = setup()
    await run(s, 'toggle_group', { group: 'gates' })
    s.store.setViewer('p_mara')
    expect((await state(s)).filters.groups).toEqual([])
  })
  it('a viewer (read-only role) can filter and page', async () => {
    const s = setup('p_tom')
    expect((await run(s, 'toggle_group', { group: 'questions' })).ok).toBe(true)
    expect((await state(s)).filters.groups).toEqual(['questions'])
    expect((await run(s, 'show_older')).ok).toBe(true)
    expect((await run(s, 'clear_filters')).ok).toBe(true)
  })
  it('ignores an unknown group, person or agent', async () => {
    const s = setup()
    await run(s, 'toggle_group', { group: 'nope' })
    await run(s, 'toggle_person', { id: 'p_nobody' })
    await run(s, 'toggle_agent', { id: 'ghost' })
    expect((await state(s)).filters).toEqual({ groups: [], people: [], agents: [], q: '' })
  })
  it('an empty result is told apart from no events at all', async () => {
    const s = setup()
    await run(s, 'search', { formData: { q: 'zzz-no-such-thing' } })
    const st = await state(s)
    expect(st.timeline).toEqual([])
    expect(JSON.stringify(st.view)).toMatch(/No events match/)
  })
})

describe('activity today card and by-ticket table', () => {
  it('counts today live: events and events by agents', async () => {
    const s = setup()
    const st = await state(s)
    const today = st.today
    expect(today.events).toBeGreaterThan(10)
    expect(today.byAgents).toBeGreaterThan(0)
    expect(st.todaySummary).toBe(`${today.events} events, ${today.byAgents} by agents`)
    await comment(s, 'DEMO-0043', 'one more')
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T1', actor: AGENT })
    const after = await state(s)
    expect(after.today.events).toBe(today.events + 2)
    expect(after.today.byAgents).toBe(today.byAgents + 1)
  })
  it('the card is not narrowed by the viewer filters', async () => {
    const s = setup()
    const all = (await state(s)).todaySummary
    await run(s, 'toggle_group', { group: 'gates' })
    expect((await state(s)).todaySummary).toBe(all)
  })
  it('merges a run only when its events are close in time', async () => {
    const s = setup()
    tick()
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T1', actor: AGENT })
    tick(3600)
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T2', actor: AGENT })
    const top = (await state(s)).timeline.slice(0, 2)
    expect(top.map((r) => r.count)).toEqual([1, 1])
  })
  it('says "1 event" for one', async () => {
    const store = createMockStore({ persist: false })
    const ws = store.workspaces.find((w) => w.prefix === 'INT')!.id
    installAndGrant(store, ws, 'activity')
    store.setViewer(store.workspaces.find((w) => w.id === ws)!.members[0].person)
    const api = createApi(createMockTransport(store, { latency: false }))
    const st = (await api.getAddonState(ws, 'activity')) as unknown as State
    expect(st.todaySummary).toMatch(/^\d+ events?, \d+ by agents?$|^No events today$/)
  })
  it('by ticket: events today and last actor, most active first', async () => {
    const s = setup()
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T1', actor: AGENT })
    await comment(s, 'DEMO-0044', 'hi')
    const rows = (await state(s)).ticketRows
    const r = rows.find((x) => x.ticket === 'DEMO-0044')!
    expect(r.today).toBeGreaterThanOrEqual(2)
    expect(r.last_actor).toBe('Severin')
    const counts = rows.map((x) => x.today)
    expect([...counts].sort((a, b) => b - a)).toEqual(counts)
  })
})

describe('activity visibility', () => {
  const hide = (s: S) => {
    for (const k of ['DEMO-0041', 'DEMO-0043']) (s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(k)!.visibility = { restricted: ['p_sev'] }
  }
  it('no event, count, table row or filter reveals a ticket the viewer cannot see', async () => {
    const s = setup('p_mara')
    const full = await state(setup('p_sev'))
    hide(s)
    const st = await state(s)
    const json = JSON.stringify(st)
    for (const k of ['DEMO-0041', 'DEMO-0043']) expect(json).not.toContain(k)
    for (const t of ['Load tariff tables as dbt seeds', 'Add billing reconciliation tests']) expect(json).not.toContain(t)
    expect(st.total).toBeLessThan(full.total)
  })
  it('today count leaves out hidden-ticket events for an outsider', async () => {
    const s = setup('p_mara')
    const open = (await state(s)).today.events
    hide(s)
    expect((await state(s)).today.events).toBeLessThan(open)
  })
  it('the owner of a restricted ticket still sees it', async () => {
    const s = setup('p_sev')
    hide(s)
    expect(JSON.stringify(await state(s))).toContain('DEMO-0043')
  })
  it('a collapsed run never merges hidden and visible events (counts are the viewer\'s)', async () => {
    const s = setup('p_mara')
    hide(s)
    const st = await state(s)
    expect(st.timeline.every((r) => !r.ticket || !['DEMO-0041', 'DEMO-0043'].includes(r.ticket))).toBe(true)
  })
})

describe('activity workspace events', () => {
  const seedWs = (s: S) => {
    tick(3600)
    // An hour apart, so none of them is merged into a run.
    const add = (input: Parameters<S['store']['appendWs']>[1]) => {
      s.store.appendWs(s.ws, input)
      tick(3600)
    }
    add({ type: 'grant.issued', grant: 'gr_secret1', person: 'p_sev', scope: 'all', until: '2026-10-10T00:00:00Z', hours: 8, sessions: [], presence: 'touchid' })
    add({ type: 'member.added', person: 'p_x', name: 'Xavier', role: 'member' })
    add({ type: 'addon.granted', name: 'quick', version: '0.1.0', package_sha256: 'abc', capabilities: [], viewer_actions: [] })
    add({ type: 'view.saved', view: 'v1', name: 'My open work', shared: false, params: {} })
    add({ type: 'addon.enabled', name: 'quick' })
  }
  const sensitive = (r: Row) => /^(grant|member)\./.test(r.summary) || /grant|member/i.test(r.summary) || r.title.includes('Xavier')
  it('viewers and members see no grant, member or addon-grant events', async () => {
    for (const v of ['p_tom']) {
      const s = setup(v)
      seedWs(s)
      const st = await state(s)
      const json = JSON.stringify(st)
      expect(json).not.toContain('gr_secret1')
      expect(JSON.stringify(st.timeline)).not.toContain('Xavier')
      expect(st.timeline.some((r) => r.summary.includes('granted'))).toBe(false)
      expect(st.timeline.some((r) => r.group === 'workspace' && r.summary.includes('saved view'))).toBe(true)
      expect(st.timeline.some((r) => r.summary.includes('enabled'))).toBe(true)
    }
  })
  it('owners and maintainers see them, without grant ids or session ids', async () => {
    for (const v of ['p_sev', 'p_mara']) {
      const s = setup(v)
      seedWs(s)
      const st = await state(s)
      const text = st.timeline.map((r) => r.summary).join('\n')
      expect(text).toMatch(/issued a grant/)
      expect(text).toMatch(/added Xavier as member/)
      expect(text).toMatch(/granted quick/)
      expect(JSON.stringify(st)).not.toContain('gr_secret1')
      expect(st.timeline.some(sensitive)).toBe(true)
    }
  })
  it('hidden sensitive events do not count in totals or the Today card for a viewer', async () => {
    const s = setup('p_tom')
    const before = await state(s)
    seedWs(s)
    const after = await state(s)
    expect(after.today.events - before.today.events).toBe(2) // view.saved and addon.enabled only
  })
})

describe('every event type has a one-line summary', () => {
  const types = [
    'ticket.created', 'status.changed', 'labels.changed', 'claim.taken', 'claim.released', 'lease.taken', 'lease.released', 'task.done', 'artifact.added',
    'question.asked', 'question.answered', 'gate.approved', 'gate.changes_requested', 'gate.invalidated', 'verdict.given', 'handoff.written', 'section.edited',
    'log.added', 'comment.added', 'people.set', 'agent.refused', 'github.pr_linked', 'github.imported', 'publish.shared', 'publish.revoked', 'publish.decided',
    'estimate.set', 'usage.recorded', 'records.committed', 'records.pushed', 'quick.made_ticket', 'wiki.linked',
    'member.added', 'member.role_changed', 'member.removed', 'gate.policy_set', 'addon.installed', 'addon.granted', 'addon.enabled', 'addon.disabled', 'addon.updated',
    'addon.uninstalled', 'addon.settings_saved', 'grant.issued', 'grant.revoked', 'view.saved', 'view.deleted', 'workspace.renamed', 'bogus.type',
  ]
  it('never returns an empty, undefined or trailing-blank summary, even for sparse events', () => {
    for (const type of types) {
      const s = describeEvent({ type })
      expect(s.trim(), type).not.toBe('')
      expect(s, type).not.toMatch(/undefined|null|\[object|\s$/)
    }
  })
  it('reads naturally for the new lines', () => {
    expect(describeEvent({ type: 'labels.changed', add: [] })).not.toMatch(/labelled\s*$/)
    expect(describeEvent({ type: 'labels.changed', add: ['urgent'] })).toBe('labelled urgent')
    expect(describeEvent({ type: 'labels.changed', add: [], remove: ['old'] })).toBe('removed label old')
    expect(describeEvent({ type: 'view.saved', name: 'My open work' })).toBe('saved view "My open work"')
    expect(describeEvent({ type: 'view.deleted' })).toBe('deleted a saved view')
    expect(describeEvent({ type: 'addon.enabled', name: 'quick' })).toBe('enabled quick')
    expect(describeEvent({ type: 'addon.granted', name: 'quick' })).toBe('granted quick')
    expect(describeEvent({ type: 'grant.issued' })).toBe('issued a grant')
    expect(describeEvent({ type: 'member.removed', who: 'Tom' })).toBe('removed Tom')
    expect(describeEvent({ type: 'member.role_changed', who: 'Tom', role: 'member' })).toBe('made Tom a member')
    expect(describeEvent({ type: 'comment.added' })).toBe('commented')
  })
})
