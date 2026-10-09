import { describe, expect, it } from 'vitest'
import { ago } from './shared'

const NOW = Date.parse('2026-10-09T11:30:00Z')
const at = (offsetMs: number) => new Date(NOW + offsetMs).toISOString()

describe('ago', () => {
  it('reads minutes, hours and days in the past', () => {
    expect(ago(at(-5 * 60_000), NOW)).toBe('5 min ago')
    expect(ago(at(-3 * 3_600_000), NOW)).toBe('3 h ago')
    expect(ago(at(-2 * 86_400_000), NOW)).toBe('2 d ago')
  })
  it('the last minute and a clock a little ahead read "just now", never "in 1 min"', () => {
    expect(ago(at(-20_000), NOW)).toBe('just now')
    expect(ago(at(0), NOW)).toBe('just now')
    expect(ago(at(45_000), NOW)).toBe('just now')
    expect(ago(at(80_000), NOW)).toBe('just now')
  })
  it('a real future time still reads "in …"', () => {
    expect(ago(at(5 * 60_000), NOW)).toBe('in 5 min')
  })
})
