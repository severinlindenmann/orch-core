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
  rows: ScheduleRow[]
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
export function useSchedules(ws: string) {
  return useQuery({
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
