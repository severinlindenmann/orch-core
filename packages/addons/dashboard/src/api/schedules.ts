// "My schedules" (U7): the person's own scheduled runs. Core owns the ops (`schedule.add`, `schedule.list`,
// `schedule.cancel`); this file holds the types, the list hook and two pure helpers: the sentence a rule reads as,
// and the hand-off action a form makes.
import { useQuery } from '@tanstack/react-query'
import { call } from './runs'
import type { HandoffAction } from './handoff'

export type ScheduleEvery = 'day' | 'week' | 'month'
export type ScheduleState = 'live' | 'cancelled' | 'expired'

/** The rule as core lists it. `every` is absent for a run that happens once (then `date` is its day). Weekdays: Monday = 0. */
export interface ScheduleRule {
  every?: ScheduleEvery
  at: string
  tz: string
  weekdays?: number[]
  day?: number
  date?: string
}

export interface ScheduleOutcome {
  /** The occurrence it was due, ISO UTC. */
  at: string
  outcome: 'ran' | 'failed' | 'did_not_run'
  reason: string | null
}

/** A row as the dashboard server sends it (`GET /api/w/{ws}/schedules`, server/schedules.py). */
export interface ScheduleRow {
  id: string
  /** The ticket key. */
  ticket: string
  title: string | null
  rule: ScheduleRule
  ends: string | null
  /** When it runs next, ISO UTC; null for one that is over. Absent only from a host that does not say. */
  next_run?: string | null
  /** The latest settled run: when it was due, how it ended, and (for a skipped one) why, from core's closed list. */
  last_outcome?: ScheduleOutcome | null
  brief: string
  label: string | null
  /** Hours each run may take (1..8); null when the server could not read it. */
  run_hours: number | null
  /** The longest a run really lasts: `run_hours` capped by the setter's grant_hours. */
  max_run_hours?: number | null
  state: ScheduleState
  mine: boolean
  /** The setter or an owner (the model's rule). */
  can_cancel: boolean
}

export interface ScheduleList {
  /** False when the workspace has not turned the Schedules add-on on. */
  available: boolean
  role: string | null
  can: { add: boolean; cancel: boolean }
  /** The viewer's own grant_hours cap on a run, when the server knows it (from their schedules); else null. */
  grant_hours_cap?: number | null
  /** The host's minimum scheduling lead in seconds (core's caps of this workspace); absent from an older server. */
  min_lead_s?: number | null
  rows: ScheduleRow[]
}

export { DEFAULT_MIN_LEAD_S, leadSentence, leadText, scheduleDenial } from './scheduleLead'

/** The wall clock of a time zone at an instant: "YYYY-MM-DD" and "HH:MM" (the browser's zone if the name is unknown). */
export function wallClock(tz: string, at: Date): { date: string; time: string } {
  const opts: Intl.DateTimeFormatOptions = { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }
  let parts: Intl.DateTimeFormatPart[]
  try {
    parts = new Intl.DateTimeFormat('en-CA', { ...opts, timeZone: tz }).formatToParts(at)
  } catch {
    parts = new Intl.DateTimeFormat('en-CA', opts).formatToParts(at)
  }
  const p = (type: string) => parts.find((x) => x.type === type)?.value ?? ''
  return { date: `${p('year')}-${p('month')}-${p('day')}`, time: `${p('hour')}:${p('minute')}` }
}

/** The earliest start the host accepts, on the zone's wall clock: now plus the lead, rounded up to the next minute. */
export function earliestStart(tz: string, now: Date, leadS: number): { date: string; time: string } {
  return wallClock(tz, new Date(Math.ceil((now.getTime() + leadS * 1000) / 60_000) * 60_000))
}

/** The first time the form offers: now plus the lead, rounded up to the time picker's step (15 minutes). */
export function firstSlot(tz: string, now: Date, leadS: number, stepMin = 15): { date: string; time: string } {
  const step = stepMin * 60_000
  return wallClock(tz, new Date(Math.ceil((now.getTime() + leadS * 1000) / step) * step))
}

/** The first run of the form's rule after now, as "YYYY-MM-DD HH:MM" on its zone's wall clock; null when there is none
 * (a once-run in the past). Weekdays: Monday = 0; a monthly day past the month's end is its last day (core's rule). */
export function firstRun(form: ScheduleForm, now: Date): string | null {
  const t = wallClock(form.tz || 'UTC', now)
  const current = `${t.date} ${t.time}`
  if (form.repeat === 'once') {
    const at = `${form.date ?? ''} ${form.time}`
    return at > current ? at : null
  }
  const [y, m, d] = t.date.split('-').map(Number)
  for (let i = 0; i < 400; i++) {
    const day = new Date(Date.UTC(y, m - 1, d + i))
    const dow = (day.getUTCDay() + 6) % 7
    const dom = day.getUTCDate()
    const last = new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth() + 1, 0)).getUTCDate()
    const hit =
      form.repeat === 'day' ||
      (form.repeat === 'week' && form.weekdays.includes(dow)) ||
      (form.repeat === 'month' && (form.day === dom || (dom === last && form.day > last)))
    const at = `${day.toISOString().slice(0, 10)} ${form.time}`
    if (hit && at > current) return at
  }
  return null
}

/** The host would refuse this form: its first run is before now plus the minimum lead (or there is none). */
export function startsTooSoon(form: ScheduleForm, now: Date, leadS: number): boolean {
  const first = firstRun(form, now)
  const e = earliestStart(form.tz || 'UTC', now, leadS)
  return first === null || first < `${e.date} ${e.time}`
}

/** The line under a row for a run that was skipped, in plain words; null when the last run was not skipped. */
export function skippedText(last: ScheduleOutcome | null | undefined): string | null {
  if (last?.outcome !== 'did_not_run') return null
  const why: Record<string, string> = {
    busy: 'you had work running on this ticket',
    claimed_by_other: 'someone else was working on this ticket',
    access_lost: 'you no longer have access to this ticket',
    addon_disabled: 'Schedules was turned off',
    role_lost: 'your role no longer lets you run agents',
    device_lost: 'your device was no longer trusted',
    ticket_closed: 'the ticket was closed',
    missed: 'it could not start in time',
  }
  const reason = last.reason ? why[last.reason] : undefined
  return typeof reason === 'string' ? `Skipped last time: ${reason}.` : 'Skipped last time.'
}

export const scheduleKey = (ws: string) => ['schedules', ws] as const

/** The person's schedules in one workspace (`schedule.list`). */
export function useSchedules(ws: string, enabled = true) {
  return useQuery({
    enabled,
    queryKey: scheduleKey(ws),
    queryFn: () => call<ScheduleList>('GET', `/api/w/${encodeURIComponent(ws)}/schedules`),
  })
}

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "Mon 2 Nov" from "2025-11-02", or null when the text is not a date. */
function dayText(date: string | undefined): string | null {
  const m = /^(\d{4})-(\d\d)-(\d\d)/.exec(date ?? '')
  if (!m) return null
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  if (Number.isNaN(d.getTime())) return null
  return `${DAYS[(d.getUTCDay() + 6) % 7]} ${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`
}

function listText(items: string[]): string {
  return items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

/** One sentence for a rule: "Runs once on Mon 2 Nov at 09:00", "Runs every day at 09:00", "Runs every week on Mon and Wed at 09:00". */
export function scheduleSummary(rule: ScheduleRule): string {
  const at = `at ${rule.at}`
  if (rule.every === 'day') return `Runs every day ${at}`
  if (rule.every === 'week') {
    const names = [...new Set(rule.weekdays ?? [])].sort((a, b) => a - b).map((n) => DAYS[n]).filter(Boolean)
    return names.length ? `Runs every week on ${listText(names)} ${at}` : `Runs every week ${at}`
  }
  if (rule.every === 'month') return rule.day ? `Runs every month on day ${rule.day} ${at}` : `Runs every month ${at}`
  const day = dayText(rule.date)
  return day ? `Runs once on ${day} ${at}` : `Runs once ${at}`
}

export type ScheduleRepeat = 'once' | 'day' | 'week' | 'month'

/** What the "Schedule a run" form holds. `date` is YYYY-MM-DD (once only), `time` is HH:MM, weekdays are Monday = 0. */
export interface ScheduleForm {
  ticket: string
  brief: string
  repeat: ScheduleRepeat
  date?: string
  time: string
  weekdays: number[]
  day: number
  tz: string
  /** Hours each run may take; absent means the host's default (8). Undo of a cancel carries the old value. */
  run_hours?: number | null
}

export type ScheduleAddAction = Extract<HandoffAction, { action: 'schedule_add' }>

/** The action the form signs. A once-run's `at` carries its date ("YYYY-MM-DD HH:MM", what `schedule.add` takes); a repeating one carries the time only. */
export function scheduleAddAction(form: ScheduleForm): ScheduleAddAction {
  const action: ScheduleAddAction = {
    action: 'schedule_add',
    key: form.ticket,
    brief: form.brief,
    at: form.repeat === 'once' ? `${form.date ?? ''} ${form.time}` : form.time,
    tz: form.tz || 'UTC',
  }
  if (form.repeat !== 'once') action.every = form.repeat
  if (form.repeat === 'week') action.weekdays = [...new Set(form.weekdays)].sort((a, b) => a - b)
  if (form.repeat === 'month') action.day = form.day
  if (form.run_hours != null) action.run_hours = form.run_hours
  return action
}
