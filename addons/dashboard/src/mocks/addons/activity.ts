import { atLeast, roleOf } from '@/api/permissions'
import type { Actor } from '@/api/types'
import { describeEvent } from '../derive'
import { findHarness, harnessForAgent } from '@/api/harnesses'
import { fmtWhen } from '@/lib/time'
import { canSeeTicket, invalid, notFound, registerAddon, type AddonCtx } from './registry'

// activity: the workspace-wide timeline (v1 D11).
//  - Built in view() from store.eventsOf over the workspace's tickets plus store.wsEventsOf. A ticket's events are listed
//    only to people who can see that ticket. Grant, member and addon-grant events are listed only to owners and
//    maintainers (they say who may do what); everyone else sees the rest of the workspace log.
//  - Dense by design: grouped by day, a run of consecutive events by one actor on one ticket is one row with a count
//    ("claude-code · DEMO-0043 · 5 task updates"), as long as they are within 30 minutes of each other, and the default view is the most recent 30 rows with "Show older".
//  - The whole page is `view().page`: a headline, ONE filter bar (period, type, person, search), a Timeline / By ticket switch
//    and the chosen view. Period, filters, view and page size are the viewer's own (`state.nav[viewer]`). Every count on the
//    page uses the chosen period; type and person counts are faceted (they respect the other filters), the headline does not.
//  - Live: view() is read-only. A viewer's position (`nav.seen`: newest seq per source, plus the dataset it was taken in)
//    is recorded by their first navigation action (apply, switching view, Show older, ...) and by `show_new`. From then on
//    later events are not listed; they are offered as "Show N new events", so rows never shift while someone reads.
//    Without a position (first visit, after a dataset switch or reset) everything counts as seen: nothing is new.
//  - The Today card is a roll-up of everything the viewer can see; filters and the freeze do not narrow it.

const PAGE = 30
/** Events further apart than this are never merged into one row. */
const RUN_GAP_MS = 30 * 60_000
const TABLE_ROWS = 12

export const GROUPS = [
  { id: 'status', label: 'Status', noun: 'ticket updates' },
  { id: 'gates', label: 'Gates', noun: 'gate events' },
  { id: 'questions', label: 'Questions', noun: 'question events' },
  { id: 'tasks', label: 'Tasks', noun: 'task updates' },
  { id: 'artifacts', label: 'Artifacts', noun: 'artifact updates' },
  { id: 'addons', label: 'Addons', noun: 'addon events' },
  { id: 'workspace', label: 'Workspace', noun: 'workspace events' },
] as const
type Group = (typeof GROUPS)[number]['id']

interface Entry {
  at: string
  seq: number
  src: string
  day: string
  /** Display name: a member's name, an agent's id, "orch", an addon's id. */
  actor: string
  /** The id a filter matches on (person id or agent id); host and addon actors cannot be filtered on. */
  actorId?: string
  kind: Actor['kind']
  ticket?: string
  group: Group
  summary: string
  type: string
}
interface Row {
  id: string
  day: string
  at: string
  actor: string
  kind: Actor['kind']
  ticket?: string
  group: Group | 'mixed'
  count: number
  summary: string
  title: string
  subtitle: string
}
type Period = 'today' | 'week' | 'all'
const PERIODS: { id: Period; label: string; word: string }[] = [
  { id: 'today', label: 'Today', word: 'today' },
  { id: 'week', label: '7 days', word: 'in the last 7 days' },
  { id: 'all', label: 'All', word: 'in total' },
]
interface Nav {
  period: Period
  /** 'all' or a group id. */
  type: string
  /** 'everyone', 'p:<person id>' or 'a:<agent id>'. */
  person: string
  q: string
  pages: number
  view: 'timeline' | 'ticket'
  /** Newest seq seen per source when the viewer last took a position; null: none taken, so nothing is new. */
  seen: Record<string, number> | null
  /** The demo dataset `seen` was taken in; a position from another dataset is no position. */
  seenIn: string | null
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

function groupOf(type: string, ws: boolean): Group {
  if (type === 'gate.policy_set') return 'workspace' // a workspace rule, not a ticket gate event
  if (type.startsWith('gate.') || type === 'verdict.given') return 'gates'
  if (type.startsWith('question.')) return 'questions'
  if (type.startsWith('lease.') || type.startsWith('claim.') || type.startsWith('task.') || type === 'agent.refused' || type === 'handoff.written') return 'tasks'
  if (type.startsWith('artifact.')) return 'artifacts'
  if (/^(publish|estimate|github|usage|records|quick|wiki|factory|schedules|land|links|addon)\./.test(type)) return 'addons'
  return ws ? 'workspace' : 'status'
}

/** Grant, member and addon-grant events tell who may do what: owners and maintainers only. */
const isSensitive = (type: string) => type.startsWith('grant.') || type.startsWith('member.') || type === 'addon.granted' || type === 'addon.action_signed' || type === 'addon.decided' || type.startsWith('skill.') || type === 'terminal.shell_opened' || type.startsWith('connection.')

function dayLabel(day: string, today: string): string {
  const d = new Date(`${day}T00:00:00Z`)
  const yesterday = new Date(new Date(`${today}T00:00:00Z`).getTime() - 86_400_000).toISOString().slice(0, 10)
  const date = `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
  if (day === today) return `Today · ${date}`
  if (day === yesterday) return `Yesterday · ${date}`
  return date
}

const navOf = (state: Record<string, unknown>, viewer: string): Nav => {
  const n = ((state.nav ?? {}) as Record<string, Partial<Nav>>)[viewer] ?? {}
  return {
    period: PERIODS.some((p) => p.id === n.period) ? n.period! : 'today',
    type: n.type ?? 'all',
    person: n.person ?? 'everyone',
    q: n.q ?? '',
    pages: n.pages && n.pages > 0 ? n.pages : 1,
    view: n.view === 'ticket' ? 'ticket' : 'timeline',
    seen: n.seen ?? null,
    seenIn: n.seenIn ?? null,
  }
}
function setNav(state: Record<string, unknown>, viewer: string, patch: Partial<Nav>): void {
  const all = (state.nav ??= {}) as Record<string, Nav>
  all[viewer] = { ...navOf(state, viewer), ...patch }
}
const filtered = (n: Nav) => n.type !== 'all' || n.person !== 'everyone' || n.q !== ''
const sourceKey = (e: Entry) => e.src
/** The newest seq per source in `list`: where the viewer's reading position is. */
function positionOf(list: Entry[]): Record<string, number> {
  const seen: Record<string, number> = {}
  for (const e of list) seen[sourceKey(e)] = Math.max(seen[sourceKey(e)] ?? 0, e.seq)
  return seen
}
const isNew = (e: Entry, seen: Record<string, number> | null) => !!seen && e.seq > (seen[sourceKey(e)] ?? 0)
/** The position to record now (actions only). */
const takePosition = (c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>) => ({ seen: positionOf(entriesOf(c)), seenIn: c.store.dataset })
/** Keep a position the viewer already has; take one if they have none (or from another dataset). */
const ensurePosition = (ctx: AddonCtx) => {
  const n = navOf(ctx.state, ctx.viewer)
  if (!n.seen || n.seenIn !== ctx.store.dataset) setNav(ctx.state, ctx.viewer, takePosition(ctx))
}

/** Every event the viewer may see, newest first. */
function entriesOf(c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>): Entry[] {
  const w = c.store.workspaces.find((x) => x.id === c.ws)
  const names = new Map((w?.members ?? []).map((m) => [m.person, m.name]))
  // Someone removed later is no longer a member: their name is still in the member.added event.
  for (const e of c.store.wsEventsOf(c.ws)) if (e.type === 'member.added' && typeof e.person === 'string' && typeof e.name === 'string' && !names.has(e.person)) names.set(e.person, e.name)
  const sees = atLeast(roleOf(w, c.viewer), 'maintainer')
  // Display names: a member's name, "Claude Code for Severin", an addon's title ("Estimate"), "orch".
  const who = (a: Actor) => {
    if (a.kind === 'person') return names.get(a.id) ?? a.id
    if (a.kind === 'agent') {
      const label = findHarness(harnessForAgent(a.id))?.label ?? a.id
      return a.for ? `${label} for ${names.get(a.for) ?? a.for}` : label
    }
    if (a.kind === 'addon') {
      // The title is the addon's own word for itself: its package id goes with it when they differ.
      const title = c.store.addons.find((p) => p.name === a.id)?.title
      return title && title !== a.id ? `${title} (${a.id})` : a.id
    }
    return 'orch'
  }
  const out: Entry[] = []
  const push = (e: ReturnType<typeof c.store.eventsOf>[number], src: string, ticket: string | undefined) => {
    const actorId = e.actor.kind === 'person' || e.actor.kind === 'agent' ? e.actor.id : undefined
    // Names for the summary lines (member events carry person ids).
    const named = { ...e, who: typeof e.person === 'string' ? names.get(e.person) : undefined }
    const summary = describeEvent(named).trim() || e.type
    out.push({ at: e.at, seq: e.seq, src, day: e.at.slice(0, 10), actor: who(e.actor), actorId, kind: e.actor.kind, ticket, group: groupOf(e.type, !ticket), summary, type: e.type })
  }
  for (const key of c.store.ticketKeys(c.ws)) {
    if (!canSeeTicket(c, key)) continue
    for (const e of c.store.eventsOf(key)) if (e.type !== 'people.set') push(e, key, key)
  }
  // A workspace event about a ticket (addon.decided) follows that ticket's visibility.
  for (const e of c.store.wsEventsOf(c.ws)) if ((sees || !isSensitive(e.type)) && (typeof e.ticket !== 'string' || canSeeTicket(c, e.ticket)) && seesEventKey(e, c)) push(e, '#ws', undefined)
  return out.sort((a, b) => b.at.localeCompare(a.at) || (a.src === b.src ? b.seq - a.seq : a.src.localeCompare(b.src)))
}

/** A discarded ticket is gone, so its log line carries the visibility the ticket had: only people who could see it see the line. */
function seesEventKey(e: { type: string; key?: unknown; visibleTo?: unknown }, c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>): boolean {
  if (e.type === 'ticket.discarded') {
    if (e.visibleTo === 'workspace') return true
    if (Array.isArray(e.visibleTo)) return e.visibleTo.includes(c.viewer)
    // Legacy records without a snapshot may use a surviving ticket; otherwise fail closed.
    return typeof e.key === 'string' && canSeeTicket(c, e.key)
  }
  return typeof e.key !== 'string' || canSeeTicket(c, e.key)
}

const inPeriod = (e: Entry, period: Period, today: string) =>
  period === 'all' || (period === 'today' ? e.day === today : e.day >= new Date(Date.parse(`${today}T00:00:00Z`) - 6 * 86_400_000).toISOString().slice(0, 10) && e.day <= today)
const byType = (e: Entry, n: Nav) => n.type === 'all' || e.group === n.type
const byPerson = (e: Entry, n: Nav) =>
  n.person === 'everyone' || (e.kind === 'person' && n.person === `p:${e.actorId}`) || (e.kind === 'agent' && n.person === `a:${e.actorId}`)
const bySearch = (e: Entry, n: Nav, titleOf: (k: string) => string) =>
  !n.q || `${e.actor} ${e.actorId ?? ''} ${e.ticket ?? ''} ${e.ticket ? titleOf(e.ticket) : ''} ${e.summary} ${e.type}`.toLowerCase().includes(n.q.toLowerCase())

/** Runs of consecutive events by one actor on one ticket (or in the workspace log) on one day become one row. */
function collapse(list: Entry[], titleOf: (k: string) => string, now: string): Row[] {
  const rows: Row[] = []
  let run: Entry[] = []
  const flush = () => {
    if (!run.length) return
    const first = run[0] // newest
    const groups = new Set(run.map((r) => r.group))
    const group = groups.size === 1 ? first.group : 'mixed'
    const noun = group === 'mixed' ? 'updates' : GROUPS.find((g) => g.id === group)!.noun
    const count = run.length
    const summary = count === 1 ? first.summary : `${count} ${noun}`
    const where = first.ticket ?? 'workspace'
    // The one time format: how long ago the newest event of the row was (the count says there were more).
    const time = fmtWhen(first.at, now)
    const what = count === 1 ? (first.ticket ? titleOf(first.ticket) : '') : `latest: ${first.summary}`
    rows.push({
      id: `${first.src}:${first.seq}`,
      day: first.day,
      at: first.at,
      actor: first.actor,
      kind: first.kind,
      ticket: first.ticket,
      group,
      count,
      summary,
      title: `${first.actor} · ${where} · ${summary}`,
      subtitle: what ? `${time} · ${what}` : time,
    })
    run = []
  }
  for (const e of list) {
    const p = run[0]
    const prev = run[run.length - 1]
    if (p && p.day === e.day && p.actor === e.actor && p.kind === e.kind && p.src === e.src && Date.parse(prev.at) - Date.parse(e.at) <= RUN_GAP_MS) run.push(e)
    else {
      flush()
      run = [e]
    }
  }
  flush()
  return rows
}

const WARN = new Set(['agent.refused', 'gate.changes_requested', 'gate.invalidated'])

const opt = (value: string, title: string) => ({ const: value, title })

registerAddon({
  name: 'activity',
  seed: () => ({ nav: {} }),

  view(state, c) {
    const today = c.store.now().slice(0, 10)
    const everything = entriesOf(c)
    const stored = navOf(state, c.viewer)
    const nav = stored.seenIn === c.store.dataset ? stored : { ...stored, seen: null, seenIn: null }
    const titles = new Map<string, string>()
    const titleOf = (k: string) => {
      if (!titles.has(k)) titles.set(k, c.store.ticket(k)?.title ?? '')
      return titles.get(k)!
    }
    const period = PERIODS.find((p) => p.id === nav.period)!
    const known = everything.filter((e) => !isNew(e, nav.seen))
    const inScope = known.filter((e) => inPeriod(e, nav.period, today))
    const shownEntries = inScope.filter((e) => byType(e, nav) && byPerson(e, nav) && bySearch(e, nav, titleOf))
    const newEntries = everything.filter((e) => isNew(e, nav.seen) && inPeriod(e, nav.period, today) && byType(e, nav) && byPerson(e, nav) && bySearch(e, nav, titleOf))
    const allRows = collapse(shownEntries, titleOf, c.store.now())
    const limit = PAGE * nav.pages
    const timeline = allRows.slice(0, limit)
    const hidden = allRows.length - timeline.length
    const shownEvents = timeline.reduce((n, r) => n + r.count, 0)

    // Headline: the period's events, unfiltered.
    const byAgentsInPeriod = inScope.filter((e) => e.kind === 'agent').length
    const headline = `${plural(inScope.length, 'event', 'events')} ${period.word} · ${byAgentsInPeriod} by ${byAgentsInPeriod === 1 ? 'agent' : 'agents'}`

    // Filter options: counts for the period, respecting the other filters (so a count is what you get when you pick it).
    const typeOptions = [
      opt('all', 'All types'),
      ...GROUPS.map((g) => opt(g.id, `${g.label} (${inScope.filter((e) => e.group === g.id && byPerson(e, nav) && bySearch(e, nav, titleOf)).length})`)),
    ]
    const w = c.store.workspaces.find((x) => x.id === c.ws)
    const forPerson = inScope.filter((e) => byType(e, nav) && bySearch(e, nav, titleOf))
    const people = (w?.members ?? []).map((m) => ({ value: `p:${m.person}`, name: m.name, n: forPerson.filter((e) => e.kind === 'person' && e.actorId === m.person).length }))
    const agentIds = [...new Set(everything.filter((e) => e.kind === 'agent').map((e) => e.actorId!))].sort()
    const agents = agentIds.map((id) => ({ value: `a:${id}`, name: findHarness(harnessForAgent(id))?.label ?? id, n: forPerson.filter((e) => e.kind === 'agent' && e.actorId === id).length }))
    const personOptions = [
      opt('everyone', 'Everyone'),
      ...[...people, ...agents].filter((p) => p.n > 0 || p.value === nav.person).map((p) => opt(p.value, `${p.name} (${p.n})`)),
    ]
    const form = {
      type: 'form',
      schema: {
        type: 'object',
        properties: {
          period: { type: 'string', title: 'Period', oneOf: PERIODS.map((p) => opt(p.id, p.label)) },
          type: { type: 'string', title: 'Type', oneOf: typeOptions },
          person: { type: 'string', title: 'Person', oneOf: personOptions },
          q: { type: 'string', title: 'Search', maxLength: 80 },
        },
      },
      uiSchema: { 'ui:options': { layout: 'row' }, 'ui:globalOptions': { layout: 'row' } },
      formData: { period: nav.period, type: nav.type, person: nav.person, q: nav.q },
      action: 'apply',
      live: true, // filters apply on change: no Apply button
    }
    const clear = filtered(nav) ? [{ type: 'button', label: 'Clear filters', action: 'clear_filters', variant: 'ghost' }] : []
    // One line: the headline, then the view switch. `fit` keeps each child at its own width, so Timeline / By ticket / Clear filters read as a switch, not as equal-width columns.
    const switcher = {
      type: 'stack',
      direction: 'row',
      fit: true,
      children: [
        { type: 'markdown', text: headline },
        {
          type: 'stack',
          direction: 'row',
          fit: true,
          children: [
            { type: 'button', label: 'Timeline', action: 'view_timeline', variant: nav.view === 'timeline' ? 'primary' : 'secondary' },
            { type: 'button', label: 'By ticket', action: 'view_ticket', variant: nav.view === 'ticket' ? 'primary' : 'secondary' },
            ...clear,
          ],
        },
      ],
    }

    // Timeline node: a heading and a list per day.
    const raw = new Map(shownEntries.map((e) => [`${e.src}:${e.seq}`, e]))
    const timelineNodes: unknown[] = [
      { type: 'markdown', text: `### Timeline\n\nShowing ${shownEvents} of ${shownEntries.length}${filtered(nav) ? ' matching' : ''}` },
    ]
    if (newEntries.length) timelineNodes.push({ type: 'button', label: `Show ${plural(newEntries.length, 'new event', 'new events')}`, action: 'show_new', variant: 'secondary' })
    for (const day of [...new Set(timeline.map((r) => r.day))]) {
      const rows = timeline.filter((r) => r.day === day)
      const events = rows.reduce((n, r) => n + r.count, 0)
      timelineNodes.push({ type: 'markdown', text: `#### ${dayLabel(day, today)} · ${plural(events, 'event', 'events')}` })
      timelineNodes.push({
        type: 'list',
        items: rows.map((r) => {
          const e = raw.get(r.id)
          return {
            title: r.title,
            subtitle: r.subtitle,
            badge: r.group === 'mixed' ? 'Mixed' : GROUPS.find((g) => g.id === r.group)!.label,
            ...(r.count === 1 && e && WARN.has(e.type) ? { status: 'warn' as const } : {}),
          }
        }),
      })
    }
    if (!timeline.length) {
      timelineNodes.push(
        everything.length
          ? { type: 'alert', tone: 'info', title: filtered(nav) ? 'No events match these filters' : `No events ${period.word}`, text: filtered(nav) ? 'Clear the filters to see everything again.' : 'Pick a longer period to see earlier events.' }
          : { type: 'alert', tone: 'info', title: 'No activity yet', text: 'Events from tickets and the workspace appear here as they happen.' },
      )
    }
    if (hidden > 0) timelineNodes.push({ type: 'button', label: 'Show older', action: 'show_older', variant: 'secondary' })

    // By ticket: the period's events per ticket (never narrowed by type, person or search).
    const perTicket = new Map<string, { n: number; last: Entry }>()
    for (const e of inScope) {
      if (!e.ticket) continue
      const cur = perTicket.get(e.ticket)
      if (cur) cur.n++
      else perTicket.set(e.ticket, { n: 1, last: e })
    }
    const ticketRows = [...perTicket]
      .sort((a, b) => b[1].n - a[1].n || b[1].last.at.localeCompare(a[1].last.at))
      .slice(0, TABLE_ROWS)
      .map(([ticket, v]) => ({ ticket, title: titleOf(ticket), events: v.n, last_actor: v.last.actor, last: fmtWhen(v.last.at, c.store.now()) }))
    const ticketNodes: unknown[] = [
      { type: 'markdown', text: `### By ticket, ${period.word}` },
      {
        type: 'table',
        columns: [
          { key: 'ticket', label: 'Ticket' },
          { key: 'title', label: 'Title' },
          { key: 'events', label: 'Events' },
          { key: 'last_actor', label: 'Last actor' },
          { key: 'last', label: 'At' },
        ],
        rows: ticketRows,
        empty: `No ticket events ${period.word}.`,
      },
    ]

    // Roll-up for the Today card (never narrowed, never frozen).
    const todays = everything.filter((e) => e.day === today)
    const byAgents = todays.filter((e) => e.kind === 'agent').length
    const todaySummary = todays.length ? `${plural(todays.length, 'event', 'events')}, ${byAgents} by ${byAgents === 1 ? 'agent' : 'agents'}` : 'No events today'
    const last = everything[0]

    return {
      timeline,
      total: allRows.length,
      hidden,
      hasMore: hidden > 0,
      newEvents: newEntries.length,
      period: nav.period,
      activeView: nav.view,
      filters: { type: nav.type, person: nav.person, q: nav.q },
      headline,
      counts: { period: inScope.length, byAgents: byAgentsInPeriod, matching: shownEntries.length, shown: shownEvents },
      typeOptions,
      personOptions,
      page: {
        type: 'stack',
        children: [
          switcher,
          form,
          ...(nav.view === 'timeline' ? timelineNodes : ticketNodes),
        ],
      },
      today: { events: todays.length, byAgents },
      todaySummary,
      todayHint: last ? `Latest: ${last.actor}, ${fmtWhen(last.at, c.store.now())}` : 'Nothing has happened yet',
      ticketRows,
    }
  },

  actions: {
    apply(ctx) {
      const f = (ctx.body.formData ?? {}) as { period?: unknown; type?: unknown; person?: unknown; q?: unknown }
      const period = String(f.period ?? 'today')
      if (!PERIODS.some((p) => p.id === period)) return invalid('Pick Today, 7 days or All.')
      const type = String(f.type ?? 'all')
      if (type !== 'all' && !GROUPS.some((g) => g.id === type)) return notFound('No such type.')
      const person = String(f.person ?? 'everyone')
      if (person !== 'everyone') {
        const id = person.slice(2)
        const w = ctx.store.workspaces.find((x) => x.id === ctx.ws)
        const ok = person.startsWith('p:') ? !!w?.members.some((m) => m.person === id) : person.startsWith('a:') && entriesOf(ctx).some((e) => e.kind === 'agent' && e.actorId === id)
        if (!ok) return notFound('No such person.')
      }
      const q = typeof f.q === 'string' ? f.q.replace(/[\r\n]+/g, ' ').trim().slice(0, 80) : ''
      setNav(ctx.state, ctx.viewer, { period: period as Period, type, person, q, pages: 1, ...takePosition(ctx) })
      return { ok: true, message: 'Filters applied.', changed: true }
    },
    // Buttons cannot carry arguments, so each view has its own action.
    view_timeline(ctx) {
      ensurePosition(ctx)
      setNav(ctx.state, ctx.viewer, { view: 'timeline' })
      return { ok: true, message: 'Showing the timeline.', changed: true }
    },
    view_ticket(ctx) {
      ensurePosition(ctx)
      setNav(ctx.state, ctx.viewer, { view: 'ticket' })
      return { ok: true, message: 'Showing events by ticket.', changed: true }
    },
    show_new(ctx) {
      setNav(ctx.state, ctx.viewer, { pages: 1, ...takePosition(ctx) })
      return { ok: true, message: 'Showing new events.', changed: true }
    },
    clear_filters(ctx) {
      ensurePosition(ctx)
      setNav(ctx.state, ctx.viewer, { type: 'all', person: 'everyone', q: '', pages: 1 })
      return { ok: true, message: 'Filters cleared.', changed: true }
    },
    show_older(ctx) {
      ensurePosition(ctx)
      const n = navOf(ctx.state, ctx.viewer)
      setNav(ctx.state, ctx.viewer, { pages: n.pages + 1 })
      return { ok: true, message: 'Showing older events.', changed: true }
    },
  },
})
