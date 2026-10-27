import { describe, expect, it } from 'vitest'
import { scheduleAddAction, scheduleSummary } from './schedules'

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
})
