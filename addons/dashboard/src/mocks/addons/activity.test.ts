import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createApi } from '@/api/client'
import { createMockTransport } from '@/api/transport'
import { describeEvent } from '@/mocks/derive'
import { createMockStore } from '@/mocks/store'
import { installAndGrant } from '@/test/installAddon'
import { refused } from '@/test/refused'

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
interface Opt {
  const: string
  title: string
}
interface Node {
  type: string
  text?: string
  label?: string
  action?: string
  schema?: { properties: Record<string, { title: string; oneOf: Opt[] }> }
  uiSchema?: { 'ui:options'?: { layout?: string } }
  formData?: Record<string, string>
  children?: Node[]
  items?: { title: string }[]
}
interface State {
  timeline: Row[]
  total: number
  hidden: number
  hasMore: boolean
  filters: { type: string; person: string; q: string }
  newEvents: number
  period: string
  activeView: string
  headline: string
  counts: { period: number; byAgents: number; matching: number; shown: number }
  typeOptions: Opt[]
  personOptions: Opt[]
  todaySummary: string
  today: { events: number; byAgents: number }
  ticketRows: { ticket: string; events: number; last_actor: string }[]
  page: Node
}
const ALL = { type: 'all', person: 'everyone', q: '' }
const everything = async (s: S) => {
  await apply(s, {})
  return state(s)
}
const apply = (s: S, f: Record<string, string>) => run(s, 'apply', { formData: { period: 'all', ...ALL, ...f } })
const countOf = (st: State, id: string) => Number(st.typeOptions.find((o) => o.const === id)!.title.match(/\((\d+)\)/)![1])
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
    const st = await everything(s)
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
    const s = setup()
    await everything(s)
    for (let i = 0; i < 6; i++) await run(s, 'show_older')
    const st = await state(s)
    const days = [...new Set(st.timeline.map((r) => r.day))]
    expect(days.length).toBeGreaterThan(2)
    expect([...days].sort().reverse()).toEqual(days)
    const heads = st.page.children!.filter((c) => c.type === 'markdown' && c.text!.startsWith('####')).map((c) => c.text!)
    expect(heads).toHaveLength(days.length)
    expect(heads[0]).toMatch(/^#### Today/)
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
  it('caps the default view at 30 rows with Show older, per viewer', async () => {
    const s = setup()
    for (let i = 0; i < 70; i++) {
      s.store.append(i % 2 ? 'DEMO-0044' : 'DEMO-0045', { type: 'log.added', text: `n${i}`, actor: i % 3 ? 'p_sev' : 'p_mara' })
    }
    const st = await state(s)
    expect(st.timeline).toHaveLength(30)
    expect(st.hasMore).toBe(true)
    expect(st.hidden).toBeGreaterThan(0)
    expect(JSON.stringify(st.page)).toContain('Show older')
    await run(s, 'show_older')
    const more = await state(s)
    expect(more.timeline).toHaveLength(60)
    s.store.setViewer('p_mara')
    expect((await state(s)).timeline).toHaveLength(30)
  })
  it('stops offering Show older once everything is shown', async () => {
    const s = setup()
    for (let i = 0; i < 6; i++) await run(s, 'show_older')
    const st = await state(s)
    expect(st.hasMore).toBe(false)
    expect(st.hidden).toBe(0)
    expect(st.timeline.length).toBe(st.total)
  })
  it('a comment elsewhere is offered as "Show 1 new event"; the rows stay put until it is taken', async () => {
    const s = setup()
    await run(s, 'show_new') // takes the viewer's position
    const before = await state(s)
    const cursor = await s.api.getCursor(s.ws)
    tick()
    await comment(s, 'DEMO-0043', 'Looks good, shipping')
    expect((await s.api.getCursor(s.ws)).cursor).toBeGreaterThan(cursor.cursor)
    const waiting = await state(s)
    expect(waiting.newEvents).toBe(1)
    expect(JSON.stringify(waiting.page)).toContain('Show 1 new event')
    expect(waiting.timeline).toEqual(before.timeline)
    expect(waiting.counts).toEqual(before.counts)
    await run(s, 'show_new')
    const top = (await state(s)).timeline[0]
    expect(top).toMatchObject({ ticket: 'DEMO-0043', actor: 'Severin' })
    expect(top.summary).toBe('logged a note')
    expect((await state(s)).newEvents).toBe(0)
  })
  it('new events are per viewer and respect the filters', async () => {
    const s = setup()
    await run(s, 'show_new')
    s.store.setViewer('p_mara')
    await run(s, 'show_new')
    tick()
    await comment(s, 'DEMO-0043', 'late')
    expect((await state(s)).newEvents).toBe(1)
    await apply(s, { type: 'gates' })
    expect((await state(s)).newEvents).toBe(0) // applying takes the new events, and a note is not a gate event
    s.store.setViewer('p_sev')
    expect((await state(s)).newEvents).toBe(1)
  })
})

describe('activity filter bar (per viewer)', () => {
  const form = (st: State) => st.page.children!.find((c) => c.type === 'form')!
  it('is one form node in row layout: Period, Type, Person, Search, Apply', async () => {
    const st = await state(setup())
    const f = form(st)
    expect(st.page.children!.filter((c) => c.type === 'form')).toHaveLength(1)
    expect(f.uiSchema!['ui:options']!.layout).toBe('row')
    expect(Object.values(f.schema!.properties).map((p) => p.title)).toEqual(['Period', 'Type', 'Person', 'Search'])
    expect(f.schema!.properties.period.oneOf.map((o) => o.title)).toEqual(['Today', '7 days', 'All'])
    expect(f.formData).toMatchObject({ period: 'today', type: 'all', person: 'everyone' })
    expect(f).toMatchObject({ action: 'apply', submitLabel: 'Apply' })
    expect(JSON.stringify(st.page)).not.toContain('Only show')
    expect(JSON.stringify(st.page)).not.toContain('Clear filters')
  })
  it('lists "All types" then the seven groups with a count for the period', async () => {
    const st = await state(setup())
    expect(st.typeOptions[0]).toEqual({ const: 'all', title: 'All types' })
    expect(st.typeOptions.slice(1).map((o) => o.title.split(' ')[0])).toEqual(['Status', 'Gates', 'Questions', 'Tasks', 'Artifacts', 'Addons', 'Workspace'])
    expect(st.typeOptions.slice(1).every((o) => /\(\d+\)$/.test(o.title))).toBe(true)
    expect(st.personOptions[0]).toEqual({ const: 'everyone', title: 'Everyone' })
    expect(st.personOptions.map((o) => o.title).join()).toMatch(/Severin \(\d+\).*claude-code \(\d+\)/)
  })
  it('the headline is the sum of the type counts for the period, and follows the period', async () => {
    const s = setup()
    let st = await state(s)
    expect(st.headline).toMatch(/^\d+ events? today · \d+ by agents?$/)
    expect(JSON.stringify(st.page.children![0])).toContain(st.headline)
    const sum = (x: State) => x.typeOptions.slice(1).reduce((n, o) => n + countOf(x, o.const), 0)
    expect(sum(st)).toBe(st.counts.period)
    await apply(s, { period: 'week' })
    const week = await state(s)
    expect(week.headline).toMatch(/in the last 7 days/)
    expect(sum(week)).toBe(week.counts.period)
    st = await everything(s)
    expect(st.headline).toMatch(/in total/)
    expect(sum(st)).toBe(st.counts.period)
    expect(st.counts.period).toBeGreaterThanOrEqual(week.counts.period)
    await apply(s, { period: 'today' })
    expect(week.counts.period).toBeGreaterThanOrEqual((await state(s)).counts.period)
  })
  it('choosing a type shows exactly the count its option promised, in the period', async () => {
    const s = setup()
    const st = await state(s)
    const n = countOf(st, 'status')
    await apply(s, { period: 'today', type: 'status' })
    const f = await state(s)
    expect(f.counts.matching).toBe(n)
    expect(f.timeline.every((r) => r.group === 'status')).toBe(true)
    expect(JSON.stringify(f.page)).toContain(`of ${n} matching`)
  })
  it('filtering to gates shows only gate events', async () => {
    const s = setup()
    await apply(s, { type: 'gates' })
    const st = await state(s)
    expect(st.timeline.length).toBeGreaterThan(0)
    expect(st.timeline.every((r) => r.group === 'gates')).toBe(true)
    expect(st.filters).toEqual({ type: 'gates', person: 'everyone', q: '' })
    expect(JSON.stringify(st.page)).toContain('Clear filters')
  })
  it('filters by person and by agent; counts respect the other filters', async () => {
    const s = setup()
    await apply(s, { person: 'a:codex' })
    let st = await state(s)
    expect(st.timeline.length).toBeGreaterThan(0)
    expect(st.timeline.every((r) => r.actor === 'codex')).toBe(true)
    const codexTasks = countOf(st, 'tasks')
    await apply(s, { person: 'a:codex', type: 'tasks' })
    st = await state(s)
    expect(st.counts.matching).toBe(codexTasks)
    await apply(s, { person: 'p:p_mara' })
    st = await state(s)
    expect(st.timeline.every((r) => r.actor === 'Mara')).toBe(true)
  })
  it('search matches summary, actor and ticket; Clear filters resets type, person and search but keeps the period', async () => {
    const s = setup()
    await apply(s, { q: 'DEMO-0043' })
    let st = await state(s)
    expect(st.timeline.length).toBeGreaterThan(0)
    expect(st.timeline.every((r) => r.ticket === 'DEMO-0043')).toBe(true)
    await apply(s, { q: 'DEMO-0043', type: 'gates' })
    await run(s, 'clear_filters')
    st = await state(s)
    expect(st.filters).toEqual(ALL)
    expect(st.period).toBe('all')
    expect(st.timeline.length).toBeGreaterThan(10)
  })
  it('stores the search query as typed (trimmed) and matches case-insensitively', async () => {
    const s = setup()
    await apply(s, { q: '  Claude-Code  ' })
    const st = await state(s)
    expect(st.filters.q).toBe('Claude-Code')
    expect(form(st).formData!.q).toBe('Claude-Code')
    expect(st.timeline.length).toBeGreaterThan(0)
  })
  it('filters belong to the viewer', async () => {
    const s = setup()
    await apply(s, { type: 'gates' })
    s.store.setViewer('p_mara')
    expect((await state(s)).filters).toEqual(ALL)
  })
  it('a viewer (read-only role) can filter, switch view and page', async () => {
    const s = setup('p_tom')
    expect((await apply(s, { type: 'questions' })).ok).toBe(true)
    expect((await state(s)).filters.type).toBe('questions')
    expect((await run(s, 'show_older')).ok).toBe(true)
    expect((await run(s, 'view_ticket')).ok).toBe(true)
    expect((await run(s, 'show_new')).ok).toBe(true)
    expect((await run(s, 'clear_filters')).ok).toBe(true)
  })
  it('refuses an unknown type, person, agent or period', async () => {
    const s = setup()
    expect(await refused(apply(s, { type: 'nope' }))).toMatchObject({ status: 404 })
    expect(await refused(apply(s, { person: 'p:p_nobody' }))).toMatchObject({ status: 404 })
    expect(await refused(apply(s, { person: 'a:ghost' }))).toMatchObject({ status: 404 })
    expect(await refused(apply(s, { period: 'decade' }))).toMatchObject({ status: 400 })
    expect((await state(s)).filters).toEqual(ALL)
  })
  it('an empty result is told apart from no events at all', async () => {
    const s = setup()
    await apply(s, { q: 'zzz-no-such-thing' })
    const st = await state(s)
    expect(st.timeline).toEqual([])
    expect(JSON.stringify(st.page)).toMatch(/No events match/)
  })
})

describe('activity reading position', () => {
  it('a state GET is read-only: the addon state is byte-identical before and after', async () => {
    const s = setup()
    const snap = () => JSON.stringify(s.store.addonState(s.ws, 'activity'))
    const before = snap()
    await state(s)
    await state(s)
    s.store.setViewer('p_mara')
    await state(s)
    expect(snap()).toBe(before)
  })
  it('on a first visit nothing is new, however many events arrive', async () => {
    const s = setup()
    tick()
    await comment(s, 'DEMO-0043', 'before the first visit')
    const st = await state(s)
    expect(st.newEvents).toBe(0)
    tick()
    await comment(s, 'DEMO-0043', 'while on the page, before any action')
    expect((await state(s)).newEvents).toBe(0)
    expect(JSON.stringify((await state(s)).page)).not.toMatch(/new events?/)
  })
  it('the first navigation action takes the position; later events are new', async () => {
    const s = setup()
    await run(s, 'view_ticket')
    tick()
    await comment(s, 'DEMO-0043', 'after the first action')
    expect((await state(s)).newEvents).toBe(1)
  })
  it('a position taken in another dataset (switch or reset) counts for nothing', async () => {
    const s = setup()
    await run(s, 'show_new')
    tick()
    await comment(s, 'DEMO-0043', 'new in this dataset')
    expect((await state(s)).newEvents).toBe(1)
    s.store.dataset = 'busy'
    expect((await state(s)).newEvents).toBe(0)
    s.store.dataset = 'normal'
  })
})

describe('activity views', () => {
  const has = (st: State, type: string) => st.page.children!.some((c) => c.type === type)
  it('renders only one view: Timeline by default, By ticket on demand', async () => {
    const s = setup()
    let st = await state(s)
    expect(st.activeView).toBe('timeline')
    expect(has(st, 'table')).toBe(false)
    expect(st.page.children!.some((c) => c.text?.startsWith('### Timeline'))).toBe(true)
    await run(s, 'view_ticket')
    st = await state(s)
    expect(has(st, 'table')).toBe(true)
    expect(has(st, 'list')).toBe(false)
    await run(s, 'view_timeline')
    expect(has(await state(s), 'table')).toBe(false)
  })
  it('the Timeline heading comes before any table', async () => {
    const st = await state(setup())
    const kinds = st.page.children!.map((c) => c.text ?? c.type)
    expect(kinds.findIndex((k) => k.startsWith('### Timeline'))).toBeGreaterThan(-1)
    expect(kinds).not.toContain('table')
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
    expect(st.counts.period).toBe(today.events) // the same period word, the same number
    await comment(s, 'DEMO-0043', 'one more')
    s.store.append('DEMO-0044', { type: 'task.done', task: 'T1', actor: AGENT })
    const after = await state(s)
    expect(after.today.events).toBe(today.events + 2)
    expect(after.today.byAgents).toBe(today.byAgents + 1)
  })
  it('the card is not narrowed by the viewer filters', async () => {
    const s = setup()
    const all = (await state(s)).todaySummary
    await apply(s, { type: 'gates', period: 'today' })
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
    await run(s, 'show_new')
    const rows = (await state(s)).ticketRows
    const r = rows.find((x) => x.ticket === 'DEMO-0044')!
    expect(r.events).toBeGreaterThanOrEqual(2)
    expect(r.last_actor).toBe('Severin')
    const counts = rows.map((x) => x.events)
    expect([...counts].sort((a, b) => b - a)).toEqual(counts)
  })
})

describe('activity visibility', () => {
  const hide = (s: S) => {
    for (const k of ['DEMO-0041', 'DEMO-0043']) (s.store as unknown as { defs: Map<string, { visibility: unknown }> }).defs.get(k)!.visibility = { restricted: ['p_sev'] }
  }
  it('no event, count, table row or filter reveals a ticket the viewer cannot see', async () => {
    const s = setup('p_mara')
    const full = await everything(setup('p_sev'))
    hide(s)
    const st = await everything(s)
    const json = JSON.stringify(st)
    for (const k of ['DEMO-0041', 'DEMO-0043']) expect(json).not.toContain(k)
    for (const t of ['Load tariff tables as dbt seeds', 'Add billing reconciliation tests']) expect(json).not.toContain(t)
    expect(st.total).toBeLessThan(full.total)
  })
  it('today count leaves out hidden-ticket events for an outsider', async () => {
    const s = setup('p_mara')
    const open = (await everything(s)).today.events
    hide(s)
    expect((await everything(s)).today.events).toBeLessThan(open)
  })
  it('the owner of a restricted ticket still sees it', async () => {
    const s = setup('p_sev')
    hide(s)
    expect(JSON.stringify(await everything(s))).toContain('DEMO-0043')
  })
  it('a collapsed run never merges hidden and visible events (counts are the viewer\'s)', async () => {
    const s = setup('p_mara')
    hide(s)
    const st = await everything(s)
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
      const st = await everything(s)
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
      const st = await everything(s)
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
    const before = await everything(s)
    seedWs(s)
    const after = await everything(s)
    expect(after.today.events - before.today.events).toBe(2) // view.saved and addon.enabled only
  })
})

describe('every event type has a one-line summary', () => {
  const types = [
    'ticket.created', 'status.changed', 'labels.changed', 'claim.taken', 'claim.released', 'lease.taken', 'lease.released', 'task.done', 'task.run', 'artifact.added',
    'question.asked', 'question.answered', 'gate.approved', 'gate.changes_requested', 'gate.invalidated', 'verdict.given', 'handoff.written', 'section.edited',
    'log.added', 'comment.added', 'people.set', 'agent.refused', 'github.pr_linked', 'github.imported', 'publish.shared', 'publish.revoked', 'publish.decided',
    'estimate.set', 'usage.recorded', 'records.committed', 'records.pushed', 'quick.made_ticket', 'wiki.linked',
    'member.added', 'member.role_changed', 'member.removed', 'gate.policy_set', 'addon.installed', 'addon.granted', 'addon.enabled', 'addon.disabled', 'addon.updated',
    'addon.uninstalled', 'addon.settings_saved', 'grant.issued', 'grant.revoked', 'agent.started', 'agent.stopped', 'view.saved', 'view.deleted', 'workspace.renamed', 'bogus.type',
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

describe('activity review follow-ups (Task 26 minors)', () => {
  it('member.removed shows the name from an earlier member.added, or "a member", never a raw person id', async () => {
    const s = setup()
    tick(3600)
    s.store.appendWs(s.ws, { type: 'member.added', person: 'p_gone', name: 'Greta', role: 'member' })
    tick(3600)
    s.store.appendWs(s.ws, { type: 'member.removed', person: 'p_gone' })
    tick(3600)
    s.store.appendWs(s.ws, { type: 'member.removed', person: 'p_unknown_id' })
    const st = await everything(s)
    const text = st.timeline.map((r) => r.summary).join('\n')
    expect(text).toMatch(/removed Greta/)
    expect(text).toMatch(/removed a member/)
    expect(JSON.stringify(st.timeline)).not.toMatch(/p_gone|p_unknown_id/)
    expect(describeEvent({ type: 'member.removed', person: 'p_raw' })).toBe('removed a member')
  })
  it('gate.policy_set is a workspace event, not a gates event', async () => {
    const s = setup()
    tick(3600)
    s.store.appendWs(s.ws, { type: 'gate.policy_set', gate: 'plan', approvers: 'maintainer', count: 2 })
    const st = await everything(s)
    const row = st.timeline.find((r) => r.summary.includes('approval rule'))!
    expect(row.group).toBe('workspace')
    await apply(s, { type: 'gates' })
    expect((await state(s)).timeline.some((r) => r.summary.includes('approval rule'))).toBe(false)
  })
})
