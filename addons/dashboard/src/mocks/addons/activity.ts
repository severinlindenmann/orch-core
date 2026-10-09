import { atLeast, roleOf } from '@/api/permissions'
import type { Actor } from '@/api/types'
import { describeEvent } from '../derive'
import { canSeeTicket, notFound, registerAddon, type AddonCtx } from './registry'

// activity: the workspace-wide timeline (v1 D11).
//  - Built in view() from store.eventsOf over the workspace's tickets plus store.wsEventsOf. A ticket's events are listed
//    only to people who can see that ticket. Grant, member and addon-grant events are listed only to owners and
//    maintainers (they say who may do what); everyone else sees the rest of the workspace log.
//  - Dense by design: grouped by day, a run of consecutive events by one actor on one ticket is one row with a count
//    ("claude-code · DEMO-0043 · 5 task updates"), as long as they are within 30 minutes of each other, and the default view is the most recent 50 rows with "Show older".
//  - Filters (type groups, people, agents, search) and the page size are the viewer's own (`state.nav[viewer]`).
//  - The Today card and the by-ticket table are roll-ups of everything the viewer can see; filters do not narrow them.

const PAGE = 50
/** Events further apart than this are never merged into one row. */
const RUN_GAP_MS = 30 * 60_000
const TABLE_ROWS = 8

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
interface Nav {
  groups: Group[]
  people: string[]
  agents: string[]
  q: string
  pages: number
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`
const hhmm = (at: string) => at.slice(11, 16)

function groupOf(type: string, ws: boolean): Group {
  if (type === 'gate.policy_set') return 'workspace' // a workspace rule, not a ticket gate event
  if (type.startsWith('gate.') || type === 'verdict.given') return 'gates'
  if (type.startsWith('question.')) return 'questions'
  if (type.startsWith('lease.') || type.startsWith('claim.') || type.startsWith('task.') || type === 'agent.refused' || type === 'handoff.written') return 'tasks'
  if (type.startsWith('artifact.')) return 'artifacts'
  if (/^(publish|estimate|github|usage|records|quick|wiki|addon)\./.test(type)) return 'addons'
  return ws ? 'workspace' : 'status'
}

/** Grant, member and addon-grant events tell who may do what: owners and maintainers only. */
const isSensitive = (type: string) => type.startsWith('grant.') || type.startsWith('member.') || type === 'addon.granted' || type === 'addon.action_signed'

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
  return { groups: n.groups ?? [], people: n.people ?? [], agents: n.agents ?? [], q: n.q ?? '', pages: n.pages && n.pages > 0 ? n.pages : 1 }
}
function setNav(state: Record<string, unknown>, viewer: string, patch: Partial<Nav>): void {
  const all = (state.nav ??= {}) as Record<string, Nav>
  all[viewer] = { ...navOf(state, viewer), ...patch }
}
const filtered = (n: Nav) => n.groups.length > 0 || n.people.length > 0 || n.agents.length > 0 || n.q !== ''

/** Every event the viewer may see, newest first. */
function entriesOf(c: Pick<AddonCtx, 'store' | 'ws' | 'viewer'>): Entry[] {
  const w = c.store.workspaces.find((x) => x.id === c.ws)
  const names = new Map((w?.members ?? []).map((m) => [m.person, m.name]))
  // Someone removed later is no longer a member: their name is still in the member.added event.
  for (const e of c.store.wsEventsOf(c.ws)) if (e.type === 'member.added' && typeof e.person === 'string' && typeof e.name === 'string' && !names.has(e.person)) names.set(e.person, e.name)
  const sees = atLeast(roleOf(w, c.viewer), 'maintainer')
  const who = (a: Actor) => (a.kind === 'person' ? (names.get(a.id) ?? a.id) : a.id)
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
  for (const e of c.store.wsEventsOf(c.ws)) if (sees || !isSensitive(e.type)) push(e, '#ws', undefined)
  return out.sort((a, b) => b.at.localeCompare(a.at) || (a.src === b.src ? b.seq - a.seq : a.src.localeCompare(b.src)))
}

function matches(e: Entry, n: Nav, titleOf: (k: string) => string): boolean {
  if (n.groups.length && !n.groups.includes(e.group)) return false
  if ((n.people.length || n.agents.length) && !((e.kind === 'person' && n.people.includes(e.actorId!)) || (e.kind === 'agent' && n.agents.includes(e.actorId!)))) return false
  if (n.q) {
    const hay = `${e.actor} ${e.ticket ?? ''} ${e.ticket ? titleOf(e.ticket) : ''} ${e.summary} ${e.type}`.toLowerCase()
    if (!hay.includes(n.q.toLowerCase())) return false
  }
  return true
}

/** Runs of consecutive events by one actor on one ticket (or in the workspace log) on one day become one row. */
function collapse(list: Entry[], titleOf: (k: string) => string): Row[] {
  const rows: Row[] = []
  let run: Entry[] = []
  const flush = () => {
    if (!run.length) return
    const first = run[0] // newest
    const last = run[run.length - 1]
    const groups = new Set(run.map((r) => r.group))
    const group = groups.size === 1 ? first.group : 'mixed'
    const noun = group === 'mixed' ? 'updates' : GROUPS.find((g) => g.id === group)!.noun
    const count = run.length
    const summary = count === 1 ? first.summary : `${count} ${noun}`
    const where = first.ticket ?? 'workspace'
    const time = count === 1 ? hhmm(first.at) : hhmm(last.at) === hhmm(first.at) ? hhmm(first.at) : `${hhmm(last.at)} to ${hhmm(first.at)}`
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

registerAddon({
  name: 'activity',
  seed: () => ({ nav: {} }),

  view(state, c) {
    const nav = navOf(state, c.viewer)
    const today = c.store.now().slice(0, 10)
    const all = entriesOf(c)
    const titles = new Map<string, string>()
    const titleOf = (k: string) => {
      if (!titles.has(k)) titles.set(k, c.store.ticket(k)?.title ?? '')
      return titles.get(k)!
    }
    const shownEntries = all.filter((e) => matches(e, nav, titleOf))
    const allRows = collapse(shownEntries, titleOf)
    const limit = PAGE * nav.pages
    const timeline = allRows.slice(0, limit)
    const hidden = allRows.length - timeline.length

    // Timeline node: a heading and a list per day.
    const raw = new Map(shownEntries.map((e) => [`${e.src}:${e.seq}`, e]))
    const children: unknown[] = []
    for (const day of [...new Set(timeline.map((r) => r.day))]) {
      const rows = timeline.filter((r) => r.day === day)
      const events = rows.reduce((n, r) => n + r.count, 0)
      children.push({ type: 'markdown', text: `### ${dayLabel(day, today)} · ${plural(events, 'event', 'events')}` })
      children.push({
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
      children.push(
        all.length
          ? { type: 'alert', tone: 'info', title: 'No events match these filters', text: 'Clear the filters to see everything again.' }
          : { type: 'alert', tone: 'info', title: 'No activity yet', text: 'Events from tickets and the workspace appear here as they happen.' },
      )
    }

    // Filters: counts over everything the viewer can see.
    const countBy = (f: (e: Entry) => boolean) => all.filter(f).length
    const w = c.store.workspaces.find((x) => x.id === c.ws)
    const toggle = (selected: boolean, action: string, args: Record<string, string>) => [
      { label: selected ? 'Remove filter' : 'Only show', action, args, variant: 'ghost' as const },
    ]
    const typeFilters = GROUPS.map((g) => {
      const on = nav.groups.includes(g.id)
      return { title: g.label, badge: String(countBy((e) => e.group === g.id)), ...(on ? { status: 'ok' as const } : {}), actions: toggle(on, 'toggle_group', { group: g.id }) }
    })
    const peopleFilters = (w?.members ?? []).map((m) => {
      const on = nav.people.includes(m.person)
      return { title: m.name, badge: String(countBy((e) => e.kind === 'person' && e.actorId === m.person)), ...(on ? { status: 'ok' as const } : {}), actions: toggle(on, 'toggle_person', { id: m.person }) }
    })
    const agentIds = [...new Set(all.filter((e) => e.kind === 'agent').map((e) => e.actorId!))].sort()
    const agentFilters = agentIds.map((id) => {
      const on = nav.agents.includes(id)
      return { title: id, badge: String(countBy((e) => e.kind === 'agent' && e.actorId === id)), ...(on ? { status: 'ok' as const } : {}), actions: toggle(on, 'toggle_agent', { id }) }
    })

    // Roll-ups (never narrowed by the filters).
    const todays = all.filter((e) => e.day === today)
    const byAgents = todays.filter((e) => e.kind === 'agent').length
    const todaySummary = todays.length ? `${plural(todays.length, 'event', 'events')}, ${byAgents} by ${byAgents === 1 ? 'agent' : 'agents'}` : 'No events today'
    const last = all[0]
    const perTicket = new Map<string, { today: number; last: Entry }>()
    for (const e of all) {
      if (!e.ticket) continue
      const cur = perTicket.get(e.ticket)
      if (!cur) perTicket.set(e.ticket, { today: e.day === today ? 1 : 0, last: e })
      else if (e.day === today) cur.today++
    }
    const ticketRows = [...perTicket]
      .filter(([, v]) => v.today > 0)
      .sort((a, b) => b[1].today - a[1].today || b[1].last.at.localeCompare(a[1].last.at))
      .slice(0, TABLE_ROWS)
      .map(([ticket, v]) => ({ ticket, title: titleOf(ticket), today: v.today, last_actor: v.last.actor, last: hhmm(v.last.at) }))

    return {
      nav: undefined,
      timeline,
      total: allRows.length,
      hidden,
      hasMore: hidden > 0,
      filters: { groups: nav.groups, people: nav.people, agents: nav.agents, q: nav.q },
      view: { type: 'stack', children },
      older: hidden > 0 ? { type: 'button', label: 'Show older', action: 'show_older', variant: 'secondary' } : { type: 'stack', children: [] },
      olderHint: hidden > 0 ? `${plural(hidden, 'older row', 'older rows')} not shown` : '',
      clear: filtered(nav) ? { type: 'button', label: 'Clear filters', action: 'clear_filters', variant: 'ghost' } : { type: 'stack', children: [] },
      searchData: { q: nav.q },
      typeFilters,
      peopleFilters,
      agentFilters,
      today: { events: todays.length, byAgents },
      todaySummary,
      todayHint: last ? `Latest: ${last.actor}, ${hhmm(last.at)}` : 'Nothing has happened yet',
      ticketRows,
    }
  },

  actions: {
    toggle_group(ctx) {
      const g = GROUPS.find((x) => x.id === ctx.body.group)
      if (!g) return notFound('No such type.')
      const n = navOf(ctx.state, ctx.viewer)
      setNav(ctx.state, ctx.viewer, { groups: n.groups.includes(g.id) ? n.groups.filter((x) => x !== g.id) : [...n.groups, g.id], pages: 1 })
      return { ok: true, message: `${g.label} filter changed.`, changed: true }
    },
    toggle_person(ctx) {
      const w = ctx.store.workspaces.find((x) => x.id === ctx.ws)
      const id = String(ctx.body.id ?? '')
      if (!w?.members.some((m) => m.person === id)) return notFound('No such person.')
      const n = navOf(ctx.state, ctx.viewer)
      setNav(ctx.state, ctx.viewer, { people: n.people.includes(id) ? n.people.filter((x) => x !== id) : [...n.people, id], pages: 1 })
      return { ok: true, message: 'Person filter changed.', changed: true }
    },
    toggle_agent(ctx) {
      const id = String(ctx.body.id ?? '')
      // Only agents that appear in what this viewer can see.
      if (!entriesOf(ctx).some((e) => e.kind === 'agent' && e.actorId === id)) return notFound('No such agent.')
      const n = navOf(ctx.state, ctx.viewer)
      setNav(ctx.state, ctx.viewer, { agents: n.agents.includes(id) ? n.agents.filter((x) => x !== id) : [...n.agents, id], pages: 1 })
      return { ok: true, message: 'Agent filter changed.', changed: true }
    },
    search(ctx) {
      const raw = (ctx.body.formData as { q?: unknown } | undefined)?.q
      const q = typeof raw === 'string' ? raw.replace(/[\r\n]+/g, ' ').trim().slice(0, 80) : ''
      setNav(ctx.state, ctx.viewer, { q, pages: 1 })
      return { ok: true, message: q ? `Searching for "${q}".` : 'Search cleared.', changed: true }
    },
    clear_filters(ctx) {
      setNav(ctx.state, ctx.viewer, { groups: [], people: [], agents: [], q: '', pages: 1 })
      return { ok: true, message: 'Filters cleared.', changed: true }
    },
    show_older(ctx) {
      const n = navOf(ctx.state, ctx.viewer)
      setNav(ctx.state, ctx.viewer, { pages: n.pages + 1 })
      return { ok: true, message: 'Showing older events.', changed: true }
    },
  },
})
