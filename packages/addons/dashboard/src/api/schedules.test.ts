import { describe, expect, it } from 'vitest'
import { ApiError } from './types'
import { clickProblem } from './click'
import { earliestStart, firstRun, firstSlot, leadText, scheduleAddAction, scheduleDenial, scheduleSummary, startsTooSoon, type ScheduleForm } from './schedules'

describe('the minimum scheduling lead (run 13)', () => {
  const now = new Date('2026-10-28T08:20:30Z') // a Wednesday
  const form = (over: Partial<ScheduleForm>): ScheduleForm => ({ ticket: 'ORCH-1', brief: 'b', repeat: 'once', date: '2026-10-28', time: '09:30', weekdays: [0], day: 1, tz: 'UTC', ...over })

  it('names the lead in words', () => {
    expect([leadText(3600), leadText(7200), leadText(1800), leadText(60), leadText(30)]).toEqual(['1 hour', '2 hours', '30 minutes', '1 minute', '30 seconds'])
  })

  it('puts the earliest start at now plus the lead on the zone clock, and the first offered time on the next quarter', () => {
    expect(earliestStart('UTC', now, 3600)).toEqual({ date: '2026-10-28', time: '09:21' }) // rounded up: 09:20:30 is too soon
    expect(firstSlot('UTC', now, 3600)).toEqual({ date: '2026-10-28', time: '09:30' })
    expect(firstSlot('Europe/Berlin', now, 3600)).toEqual({ date: '2026-10-28', time: '10:30' })
    expect(firstSlot('UTC', new Date('2026-10-28T23:30:00Z'), 3600)).toEqual({ date: '2026-10-29', time: '00:30' })
  })

  it('finds the first run of a rule as the host does', () => {
    expect(firstRun(form({}), now)).toBe('2026-10-28 09:30')
    expect(firstRun(form({ date: '2026-10-27' }), now)).toBeNull() // a once-run in the past: none
    expect(firstRun(form({ repeat: 'day', time: '08:00' }), now)).toBe('2026-10-29 08:00')
    expect(firstRun(form({ repeat: 'week', weekdays: [2], time: '09:00' }), now)).toBe('2026-10-28 09:00')
    expect(firstRun(form({ repeat: 'week', weekdays: [0], time: '09:00' }), now)).toBe('2026-11-02 09:00')
    expect(firstRun(form({ repeat: 'month', day: 31, time: '09:00' }), now)).toBe('2026-10-31 09:00')
    expect(firstRun(form({ repeat: 'month', day: 31, time: '09:00' }), new Date('2026-11-01T00:00:00Z'))).toBe('2026-11-30 09:00') // clamped
  })

  it('refuses before the host does: a first run inside the lead', () => {
    expect(startsTooSoon(form({ time: '09:15' }), now, 3600)).toBe(true) // the owner's case was 1.5 h ahead: allowed
    expect(startsTooSoon(form({ time: '09:50' }), now, 3600)).toBe(false)
    expect(startsTooSoon(form({ time: '09:15' }), now, 30)).toBe(false) // a dev sandbox lead
    expect(startsTooSoon(form({ date: '2026-10-27' }), now, 3600)).toBe(true)
    expect(startsTooSoon(form({ repeat: 'day', time: '09:00' }), now, 3600)).toBe(true) // today 09:00 comes first
    expect(startsTooSoon(form({ repeat: 'day', time: '08:00' }), now, 3600)).toBe(false) // first run is tomorrow
  })

  it('says the host refusal in plain words, in a click and in the terminal hand-off', () => {
    const raw = 'first occurrence must be after the minimum scheduling lead'
    expect(scheduleDenial('schedule.denied', raw)).toBe('A schedule must start at least 1 hour from now.')
    expect(scheduleDenial('schedule.denied', `schedule.denied: ${raw}`)).toBe('A schedule must start at least 1 hour from now.')
    expect(scheduleDenial('schedule.denied', 'ticket_closed')).toBeNull()
    expect(scheduleDenial('other.code', raw)).toBeNull()
    const p = clickProblem(new ApiError(400, { code: 'schedule.denied', message: raw, retryable: false }))
    expect(p.body).toContain('A schedule must start at least 1 hour from now.')
    expect(p.body).not.toContain('minimum scheduling lead')
    expect(p.details).toBe(raw) // the host's own words stay folded under Technical details
    expect(clickProblem(new ApiError(400, { code: 'schedule.denied', message: 'ticket_closed', retryable: false })).body).toBe('ticket_closed')
  })

  it('says the lead the server returned when it is known, not always an hour', () => {
    const raw = 'first occurrence must be after the minimum scheduling lead'
    expect(scheduleDenial('schedule.denied', raw, 30)).toBe('A schedule must start at least 30 seconds from now.')
    expect(scheduleDenial('schedule.denied', raw, undefined)).toBe('A schedule must start at least 1 hour from now.')
    const p = clickProblem(new ApiError(400, { code: 'schedule.denied', message: raw, retryable: false }), 7200)
    expect(p.body).toContain('A schedule must start at least 2 hours from now.')
    expect(p.body).not.toContain('1 hour')
  })
})

describe('scheduleSummary', () => {
  it('says a one-off run with its weekday', () => {
    expect(scheduleSummary({ at: '09:00', tz: 'UTC', date: '2026-11-02' })).toBe('Runs once on Mon 2 Nov at 09:00')
  })
  it('says a daily run', () => {
    expect(scheduleSummary({ every: 'day', at: '09:00', tz: 'UTC' })).toBe('Runs every day at 09:00')
  })
  it('lists the weekdays of a weekly run', () => {
    expect(scheduleSummary({ every: 'week', at: '09:00', tz: 'UTC', weekdays: [2, 0] })).toBe('Runs every week on Mon and Wed at 09:00')
    expect(scheduleSummary({ every: 'week', at: '18:30', tz: 'UTC', weekdays: [0, 2, 4] })).toBe('Runs every week on Mon, Wed and Fri at 18:30')
  })
  it('names the day of a monthly run', () => {
    expect(scheduleSummary({ every: 'month', at: '08:15', tz: 'UTC', day: 15 })).toBe('Runs every month on day 15 at 08:15')
  })
})

describe('scheduleAddAction', () => {
  const base = { ticket: 'ORCH-1', brief: 'Check the build', time: '09:00', weekdays: [], day: 1, tz: 'Europe/Berlin' }
  it('builds a new-ticket action without the selected ticket', () => {
    expect(scheduleAddAction({ ...base, repeat: 'day', target: 'new', title: 'Mailbox' })).toEqual({
      action: 'schedule_add', target: 'new', title: 'Mailbox', brief: 'Check the build', at: '09:00', every: 'day', tz: 'Europe/Berlin',
    })
  })
  it('keeps the existing-ticket action unchanged and omits stale title', () => {
    expect(scheduleAddAction({ ...base, repeat: 'day', target: 'existing', title: 'Mailbox' })).toEqual(scheduleAddAction({ ...base, repeat: 'day' }))
  })
  it('builds a once-run with its date and no repeat', () => {
    expect(scheduleAddAction({ ...base, repeat: 'once', date: '2026-11-02' })).toEqual({
      action: 'schedule_add', key: 'ORCH-1', brief: 'Check the build', at: '2026-11-02 09:00', tz: 'Europe/Berlin',
    })
  })
  it('builds a weekly run with sorted weekdays', () => {
    expect(scheduleAddAction({ ...base, repeat: 'week', weekdays: [4, 0] })).toEqual({
      action: 'schedule_add', key: 'ORCH-1', brief: 'Check the build', at: '09:00', every: 'week', tz: 'Europe/Berlin', weekdays: [0, 4],
    })
  })
  it('builds a monthly run with its day', () => {
    const a = scheduleAddAction({ ...base, repeat: 'month', day: 31 })
    expect(a.every).toBe('month')
    expect(a.day).toBe(31)
    expect(a.weekdays).toBeUndefined()
  })
  it('builds a daily run with neither weekdays nor day', () => {
    const a = scheduleAddAction({ ...base, repeat: 'day', weekdays: [1], day: 5 })
    expect(a.every).toBe('day')
    expect(a.weekdays).toBeUndefined()
    expect(a.day).toBeUndefined()
  })
  it('carries run_hours when the form has it (Undo of a cancel) and leaves it to the host otherwise', () => {
    expect(scheduleAddAction({ ...base, repeat: 'day', run_hours: 2 }).run_hours).toBe(2)
    expect('run_hours' in scheduleAddAction({ ...base, repeat: 'day', run_hours: null })).toBe(false)
    expect('run_hours' in scheduleAddAction({ ...base, repeat: 'day' })).toBe(false)
  })
})
