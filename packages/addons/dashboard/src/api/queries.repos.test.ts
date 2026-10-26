import { describe, expect, it } from 'vitest'
import { queries } from './queries'

// The shared addon-state query: generic polling rules, no addon names in core.
describe('addon state refresh', () => {
  const interval = (data: Record<string, unknown> | undefined) => {
    const f = queries.addonState('ws', 'any').refetchInterval as (q: { state: { data: unknown } }) => number | false
    return f({ state: { data } })
  }
  it('polls each second while moving, else at the next refresh the addon asks for, bounded to 30 s – 1 h', () => {
    expect(interval({ moving: true, nextRefreshMs: 900_000 })).toBe(1000)
    expect(interval({ nextRefreshMs: 900_000 })).toBe(900_000)
    expect(interval({ nextRefreshMs: 5 })).toBe(30_000)
    expect(interval({ nextRefreshMs: 86_400_000 })).toBe(3_600_000)
    expect(interval({ nextRefreshMs: 'soon' })).toBe(false)
    expect(interval({})).toBe(false)
    expect(interval(undefined)).toBe(false)
  })
})
