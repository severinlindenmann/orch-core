import { describe, expect, it } from 'vitest'
import { accessLine, grantLabel } from './grants'

const NOW = Date.parse('2026-10-09T11:30:00Z')

describe('grantLabel', () => {
  it('names a grant by person, scope and end, never by id', () => {
    expect(grantLabel({ scope: 'all', until: '2026-10-09T17:00:00Z' }, 'Mara', NOW)).toBe("Mara's grant (all tickets · until 17:00)")
    expect(grantLabel({ scope: 'ci', until: '2026-10-10T09:00:00Z' }, 'Severin', NOW)).toBe("Severin's grant (CI only · until 10 Oct 09:00)")
  })
})

describe('accessLine', () => {
  const min = 60_000
  const at = (m: number) => new Date(NOW + m * min).toISOString()
  it('is ok with 60 min or more left', () => {
    expect(accessLine({ until: at(61) }, NOW)).toEqual({ kind: 'ok' })
  })
  it('is ending with 15 to 60 min left', () => {
    expect(accessLine({ until: at(59) }, NOW)).toEqual({ kind: 'ending', until: at(59) })
  })
  it('is missing with under 15 min left', () => {
    expect(accessLine({ until: at(14) }, NOW)).toEqual({ kind: 'missing' })
  })
  it('is missing without a grant', () => {
    expect(accessLine(null, NOW)).toEqual({ kind: 'missing' })
  })
  it('is missing when the grant is not startable', () => {
    expect(accessLine({ until: at(120), startable: false }, NOW)).toEqual({ kind: 'missing' })
  })
})

describe('B8 access length helpers', () => {
  const h = 3_600_000
  const t0 = Date.parse('2026-10-16T10:00:00Z')
  it('names lengths as days or hours', async () => {
    const { accessLength, ACCESS_LENGTHS } = await import('./grants')
    expect(ACCESS_LENGTHS.map((l) => accessLength(l.hours))).toEqual(['8 h', '1 day', '3 days', '7 days'])
  })
  it('reminds under 24 h left, or under 25% of a grant shorter than a day', async () => {
    const { accessReminder } = await import('./grants')
    const week = { issued_at: new Date(t0).toISOString(), until: new Date(t0 + 168 * h).toISOString() }
    expect(accessReminder(week, t0 + 143 * h)).toBe(false)
    expect(accessReminder(week, t0 + 145 * h)).toBe(true)
    const short = { issued_at: new Date(t0).toISOString(), until: new Date(t0 + 8 * h).toISOString() }
    expect(accessReminder(short, t0 + 5 * h)).toBe(false)
    expect(accessReminder(short, t0 + 6.5 * h)).toBe(true)
    expect(accessReminder(short, t0 + 9 * h)).toBe(false) // ended: no reminder
  })
})
