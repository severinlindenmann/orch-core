import { describe, expect, it } from 'vitest'
import { ago } from './shared'
import { fmtAge, fmtDateTime, fmtExact, fmtSpan, plural } from '@/lib/time'

const NOW = Date.parse('2026-10-09T11:30:00Z')
const at = (offsetMs: number) => new Date(NOW + offsetMs).toISOString()

// One formatter everywhere (src/lib/time.ts): relative within a week, "9 Oct 11:36" before, UTC only in fmtExact.
describe('ago (fmtWhen)', () => {
  it('reads minutes, hours and days in the past', () => {
    expect(ago(at(-5 * 60_000), NOW)).toBe('5 min ago')
    expect(ago(at(-3 * 3_600_000), NOW)).toBe('3 h ago')
    expect(ago(at(-1 * 86_400_000), NOW)).toBe('1 day ago')
    expect(ago(at(-2 * 86_400_000), NOW)).toBe('2 days ago')
  })
  it('a week or more back is a date and a time, without UTC', () => {
    expect(ago('2026-10-01T09:05:00Z', NOW)).toBe('1 Oct 09:05')
    expect(fmtDateTime('2026-10-09T11:36:00Z')).toBe('9 Oct 11:36')
  })
  it('the last minute and any future time read "just now", never "in 6 min"', () => {
    expect(ago(at(-20_000), NOW)).toBe('just now')
    expect(ago(at(0), NOW)).toBe('just now')
    expect(ago(at(80_000), NOW)).toBe('just now')
    expect(ago(at(6 * 60_000), NOW)).toBe('just now')
  })
  it('UTC is written only for the exact instant; ages, spans and plurals', () => {
    expect(fmtExact('2026-10-04T09:30:00Z')).toBe('4 Oct 2026 09:30 UTC')
    expect(fmtAge(at(-14 * 60_000), NOW)).toBe('14 min')
    expect(fmtAge(at(60_000), NOW)).toBe('just now')
    expect(fmtSpan(2 * 3_600_000 + 5 * 60_000)).toBe('2 h 5 min')
    expect(plural(1, 'task')).toBe('1 task')
    expect(plural(3, 'task')).toBe('3 tasks')
  })
})
