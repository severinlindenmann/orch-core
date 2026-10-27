// Follow-ups (Schedules add-on, #720): what an agent promised to come back to on a ticket, and what went wrong with it.
//
// One shape for the mock and the real host (`GET /api/w/:ws/followups`; the mock puts the same object in the Schedules
// add-on's state, key `followups`). Nothing here invents a fact: a field the host does not send is absent, and the
// page draws nothing for it (webhook and event triggers appear only when the data has them). The sentences are the closed
// reason lists of the spec (docs/architecture/orch-v2-followups.md §6), written once here so every screen says the same.
import { queryOptions } from '@tanstack/react-query'
import { queries } from './queries'
import { call } from './runs'
import { hasFeature, isRealHost } from './features'
import { addonActive } from './addons'
import type { Role, Workspace } from './types'
import { fmtClock, fmtDay, fmtSpan, nowMs, plural } from '@/lib/time'

export type FollowupState = 'pending' | 'needs_person' | 'fired' | 'ran' | 'failed' | 'did_not_run' | 'cancelled'
export type FollowupTrigger = 'time' | 'orch' | 'addon' | 'webhook' | 'deadline'

export interface FollowupRow {
  id: string
  ticket: string | null
  title: string | null
  state: FollowupState
  trigger: FollowupTrigger
  due: string | null
  deadline: string | null
  set_at: string | null
  fired_at: string | null
  /** Seconds a fired follow-up started after its due time. */
  late_by: number | null
  /** The agent's note to itself (display text); null when the host withheld it (hidden characters). */
  brief: string | null
  brief_hidden?: boolean
  outcome: 'ran' | 'failed' | 'did_not_run' | null
  reason: string | null
  /** Why a deadline wake-up fired (`no_event`, `no_ring`, `addon_silent`); only when the host says. */
  fallback?: string | null
  running: boolean
  person: string | null
  mine: boolean
  /** The web-address label of a webhook trigger; only when the data has one. */
  label?: string | null
  /** "2 of 5": the place in the chain of follow-ups with no task done; only when the host says. */
  chain?: { n: number; max: number } | null
  /** Who may cancel it (its person or a manager): the host decides, a button the host would refuse is not drawn. */
  can_cancel: boolean
}

export interface FollowupsData {
  available: boolean
  role?: Role
  /** What this host can do. Reschedule and Undo are off where the host has no op for them. */
  can?: { cancel: boolean; reschedule: boolean; undo: boolean }
  rows?: FollowupRow[]
}

const w = (ws: string) => `/api/w/${encodeURIComponent(ws)}`

const realList = (ws: string) =>
  queryOptions({
    queryKey: ['followups', ws],
    queryFn: () => call<FollowupsData>('GET', `${w(ws)}/followups`),
    retry: false,
    staleTime: 5_000,
    refetchInterval: 30_000,
  })

export const followupQueries = {
  /**
   * The follow-ups of a workspace. A real host has its own route. The mock keeps them in the Schedules add-on's state, so the
   * query IS the add-on's state query (one request for every card, never one per card) with the follow-ups selected.
   */
  list: (ws: string) =>
    isRealHost()
      ? realList(ws)
      : (queryOptions({ ...queries.addonState(ws, 'schedules'), select: (s: unknown) => (s as { followups?: FollowupsData }).followups ?? { available: false } }) as unknown as ReturnType<typeof realList>),
}

/** Are follow-ups on for this workspace? Real host: the server lists `schedules` while the add-on is ON; the mock: the add-on is active. */
export function followupsOn(workspace: Pick<Workspace, 'addons' | 'id'> | undefined): boolean {
  if (!workspace) return false
  return isRealHost() ? hasFeature('schedules', workspace.id) : addonActive(workspace, 'schedules')
}

// ---- time words (local zone, never UTC)

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : NaN)

/** "Tue 27 Oct, 09:00" in the viewer's zone. */
export function wakeText(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return `${DOW[d.getDay()]} ${fmtDay(iso)}, ${fmtClock(iso)}`
}
/** "Tue 09:00": the short form for a chip. */
export function chipTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : `${DOW[d.getDay()]} ${fmtClock(iso)}`
}
/** "in 6 days", "tomorrow", "in 3 h", "in 20 min", or "overdue". */
export function inText(iso: string, now = nowMs()): string {
  const diff = ms(iso) - now
  if (Number.isNaN(diff)) return ''
  if (diff < 0) return 'overdue'
  if (diff < 3_600_000) return `in ${Math.max(1, Math.round(diff / 60_000))} min`
  if (diff < 86_400_000) return `in ${Math.round(diff / 3_600_000)} h`
  const days = Math.round(diff / 86_400_000)
  return days === 1 ? 'tomorrow' : `in ${days} days`
}
/** "7 h late" from seconds. */
export function lateText(seconds: number): string {
  return `${fmtSpan(seconds * 1000)} late`
}

// ---- kinds and groups

export type Kind = 'waiting' | 'needs_person' | 'running' | 'ran' | 'ran_late' | 'woken' | 'failed' | 'did_not_run' | 'cancelled'

export function kindOf(r: FollowupRow): Kind {
  switch (r.state) {
    case 'pending':
      return 'waiting'
    case 'needs_person':
      return 'needs_person'
    case 'fired':
      return 'running'
    case 'failed':
      return 'failed'
    case 'did_not_run':
      return 'did_not_run'
    case 'cancelled':
      return 'cancelled'
    case 'ran':
      if (r.trigger === 'deadline' || r.fallback) return 'woken'
      return r.late_by && r.late_by >= 3600 ? 'ran_late' : 'ran'
  }
}

/** The states a person has to deal with: they never go away by themselves while they are the ticket's latest follow-up. */
export const needsAPerson = (r: FollowupRow) => r.state === 'needs_person' || r.state === 'failed' || r.state === 'did_not_run'

const DAY = 86_400_000
/** The moment a row "happened" (for how long a finished one is shown). */
const happened = (r: FollowupRow) => ms(r.fired_at) || ms(r.due) || ms(r.set_at) || 0

/** The newest follow-up of each ticket (a new one replaces the pending one; an older record is history). */
export function latestByTicket(rows: readonly FollowupRow[]): Map<string, FollowupRow> {
  const out = new Map<string, FollowupRow>()
  for (const r of rows) {
    if (!r.ticket) continue
    const have = out.get(r.ticket)
    if (!have || (ms(r.set_at) || 0) > (ms(have.set_at) || 0)) out.set(r.ticket, r)
  }
  return out
}

/** What the ticket page and the board show for a ticket: its latest follow-up, while it still matters. */
export function currentFor(rows: readonly FollowupRow[] | undefined, ticket: string, now = nowMs()): FollowupRow | null {
  const r = rows ? latestByTicket(rows).get(ticket) : undefined
  if (!r) return null
  if (r.state === 'pending' || r.state === 'needs_person' || r.state === 'fired') return r
  // finished ones stay 7 days; one that needs a person stays until it is the ticket's latest no longer
  if (needsAPerson(r)) return r
  return now - happened(r) <= 7 * DAY ? r : null
}

export interface Groups {
  needs: FollowupRow[]
  waiting: FollowupRow[]
  finished: FollowupRow[]
}
/** The tab's three groups: needs a person first, then waiting (soonest first), then the last 7 days of what finished. */
export function groups(rows: readonly FollowupRow[], now = nowMs()): Groups {
  const latest = new Set([...latestByTicket(rows).values()].map((r) => r.id))
  const needs = rows.filter((r) => needsAPerson(r) && latest.has(r.id))
  const waiting = rows.filter((r) => r.state === 'pending' || r.state === 'fired').sort((a, b) => (ms(a.due ?? a.deadline) || 0) - (ms(b.due ?? b.deadline) || 0))
  const finished = rows
    .filter((r) => !needs.includes(r) && !waiting.includes(r) && r.state !== 'needs_person' && now - happened(r) <= 7 * DAY)
    .sort((a, b) => happened(b) - happened(a))
  return { needs, waiting, finished: finished }
}

export const chainText = (r: FollowupRow) => (r.chain ? `Follow-up ${r.chain.n} of ${r.chain.max}.` : '')

// ---- sentences (the closed reason lists)

export const OUTCOME_SENTENCE: Record<string, string> = {
  no_host: 'The computer running orch was off for more than a day after the follow-up was due, so it was dropped.',
  harness_failed: 'The agent program could not be started.',
  budget: 'The agent budget was used up, so the agent could not be started.',
  runner_unavailable: 'No computer was connected to run agents, so orch could not start the agent.',
  runner_restarted: 'The computer running the agent restarted while it was working.',
  host_restarted: 'orch restarted at the moment it was about to start the agent, so it did not start. It is not tried again.',
  grant_revoked: 'The permission the agent worked under was withdrawn, so it was not started.',
  ticket_moved_on: 'The ticket had moved on by then, so nothing was started.',
  no_writes: 'The session ended without writing anything to the ticket. It may have crashed or had nothing to do.',
  error: 'Something went wrong while starting the agent.',
}
export const CANCEL_SENTENCE: Record<string, string> = {
  replaced: 'The agent set a newer follow-up instead.',
  ticket_left_in_progress: 'The ticket is no longer In progress, so the wake-up was dropped.',
  claimed_by_other: 'Someone started working on the ticket by hand, so the wake-up was dropped.',
  grant_revoked: "The agent's permission was withdrawn, so the wake-up was dropped.",
  member_removed: 'The person whose agent set it left the workspace, so the wake-up was dropped.',
  addon_disabled: 'Schedules was turned off, so the wake-up was dropped.',
  chain_cap: 'orch stopped this chain: too many follow-ups in a row with no task done.',
  person: 'A person cancelled it.',
  agent: 'The agent cancelled it.',
  run_now: 'A person started the agent right away instead.',
  dismissed: 'It was dismissed.',
}
export const FALLBACK_SENTENCE: Record<string, string> = {
  no_event: 'orch had not heard that the job finished by the deadline, so the agent was started anyway to look.',
  no_ring: 'The web address was never called by the deadline, so the agent was started anyway to look.',
  addon_silent: 'The add-on never reported by the deadline, so the agent was started anyway to look.',
}
export const CHAIN_CAP_SENTENCE = 'Stopped by orch: too many follow-ups in a row with no task done, so no more are started by themselves.'

/** The one sentence under a row's badge: why it is in this state. Falls back to plain words, never to a code. */
export function reasonText(r: FollowupRow): string {
  switch (kindOf(r)) {
    case 'needs_person':
      return CHAIN_CAP_SENTENCE
    case 'woken':
      return FALLBACK_SENTENCE[r.fallback ?? 'no_event'] ?? 'The deadline came first, so the agent was started anyway to look.'
    case 'ran_late':
      return `Started ${fmtSpan((r.late_by ?? 0) * 1000)} late. orch was not running at the due time (the computer was asleep or off) and started when it woke.`
    case 'cancelled':
      return (r.reason && CANCEL_SENTENCE[r.reason]) || 'It was cancelled.'
    case 'failed':
    case 'did_not_run':
      return (r.reason && OUTCOME_SENTENCE[r.reason]) || 'No reason was recorded.'
    default:
      return ''
  }
}

/** The headline of a card or banner for a row that went wrong. */
export function headline(r: FollowupRow): string {
  switch (kindOf(r)) {
    case 'needs_person':
      return `${chainCount(r)} with no task done`
    case 'failed':
      return 'The follow-up ran but wrote nothing'
    case 'did_not_run':
      return 'Nothing was started'
    case 'woken':
      return 'The agent was woken without the event'
    case 'cancelled':
      return 'Cancelled'
    case 'running':
      return 'The agent is working now'
    case 'ran_late':
      return `Ran, ${lateText(r.late_by ?? 0)}`
    case 'ran':
      return 'Ran'
    default:
      return `The agent comes back ${r.due ? wakeText(r.due) : ''}`.trim()
  }
}
const chainCount = (r: FollowupRow) => (r.chain ? plural(r.chain.n, 'follow-up') + ' in a row' : 'Follow-ups in a row')

/** What a waiting row says it wakes on: a time, or the event or web address and the latest time (only what the data has). */
export function wakeLine(r: FollowupRow): string {
  if (r.trigger !== 'time' && r.deadline) {
    const what = r.label ? `${r.label} finishes` : 'the job finishes'
    return `Wakes when ${what}, at the latest ${wakeText(r.deadline)}.`
  }
  const at = r.due ?? r.deadline
  return at ? `Wakes ${wakeText(at)}.` : ''
}

// ---- Hide for me (per person, this browser): hides a Today card only; the ticket strip and the board chip keep the problem

const HIDE_KEY = (ws: string, person: string) => `orch.followups.hidden.${ws}.${person}`
export function hiddenIds(ws: string, person: string): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(HIDE_KEY(ws, person)) ?? '[]') as string[])
  } catch {
    return new Set()
  }
}
export function hide(ws: string, person: string, id: string): void {
  try {
    const s = hiddenIds(ws, person)
    s.add(id)
    localStorage.setItem(HIDE_KEY(ws, person), JSON.stringify([...s].slice(-200)))
  } catch {
    /* storage can be blocked: the card then simply stays */
  }
}
export function unhide(ws: string, person: string, id: string): void {
  try {
    const s = hiddenIds(ws, person)
    s.delete(id)
    localStorage.setItem(HIDE_KEY(ws, person), JSON.stringify([...s]))
  } catch {
    /* see hide */
  }
}

/** The cards Today draws: what went wrong or was stopped, not hidden by this person. Success never makes a card. */
export function todayCards(rows: readonly FollowupRow[], hidden: ReadonlySet<string>, now = nowMs()): FollowupRow[] {
  const latest = new Set([...latestByTicket(rows).values()].map((r) => r.id))
  return rows
    .filter((r) => latest.has(r.id) && !hidden.has(r.id) && (needsAPerson(r) || kindOf(r) === 'woken') && now - happened(r) <= 30 * DAY)
    .sort((a, b) => happened(b) - happened(a))
}

// ---- Reschedule quick picks (no typed times): tomorrow 09:00, in 2 days 09:00, next Monday 09:00, or a picked date and time

export interface QuickPick {
  label: string
  at: Date
}
export function quickPicks(now = nowMs()): QuickPick[] {
  const base = new Date(now)
  const at9 = (days: number) => {
    const d = new Date(base)
    d.setDate(d.getDate() + days)
    d.setHours(9, 0, 0, 0)
    return d
  }
  const toMonday = ((8 - base.getDay()) % 7) || 7
  return [
    { label: 'Tomorrow 09:00', at: at9(1) },
    { label: 'In 2 days 09:00', at: at9(2) },
    { label: 'Next Monday 09:00', at: at9(toMonday) },
  ]
}
