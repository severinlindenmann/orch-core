import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { fmtClock, fmtDateTime, fmtExact, fmtSlot, sameLocalDay } from './time'
import { grantLabel } from '@/api/grants'

// B12A N1: every time is the browser's local zone. 2026-10-20T17:59:53Z is 19:59 in Zurich (CEST, UTC+2).
describe('local-zone time formatting', () => {
  let tz: string | undefined
  beforeEach(() => { tz = process.env.TZ; process.env.TZ = 'Europe/Zurich' })
  afterEach(() => { process.env.TZ = tz })

  it('shows the local time of day, day, slot and exact instant with the zone name', () => {
    const iso = '2026-10-20T17:59:53Z'
    expect(fmtClock(iso)).toBe('19:59')
    expect(fmtSlot(iso)).toBe('Tue 19:59')
    expect(fmtDateTime(iso)).toBe('20 Oct 19:59')
    expect(fmtExact(iso)).toMatch(/^20 Oct 2026 19:59 (GMT\+2|CEST)$/)
  })
  it('rolls the day over in the local zone (the pill and the dialog agree)', () => {
    expect(fmtSlot('2026-10-17T00:11:00Z')).toBe('Sat 02:11')
    expect(fmtDateTime('2026-10-17T23:30:00Z')).toBe('18 Oct 01:30')
  })
  it('compares calendar days locally', () => {
    expect(sameLocalDay('2026-10-17T21:30:00Z', '2026-10-17T23:30:00Z')).toBe(false) // 23:30 and 01:30 next day
    expect(sameLocalDay('2026-10-17T10:00:00Z', '2026-10-17T15:00:00Z')).toBe(true)
    expect(grantLabel({ scope: 'all', until: '2026-10-17T23:30:00Z' }, 'Mara', Date.parse('2026-10-17T21:30:00Z'))).toContain('until 18 Oct 01:30')
  })
})
