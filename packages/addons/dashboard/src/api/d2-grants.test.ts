import { describe, expect, it, vi } from 'vitest'
import { reusableAccess } from './grants'

describe('D2 access renewal', () => {
  it.each([14, 15, 16])('uses host time at %s minutes left, regardless of browser clock', minutes => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2099-01-01T00:00:00Z'))
    const now = Date.parse('2026-10-28T12:00:00Z')
    const grant = { id: 'gr_demo', until: new Date(now + minutes * 60_000).toISOString(), scope: 'workable' as const, startable: true }
    const result = reusableAccess({ connected: true, enabled: true, terminals: false, now: new Date(now).toISOString(), grant })
    expect(result).toEqual(minutes >= 15 ? grant : null)
    clock.mockRestore()
  })
  it('never reuses unknown or invalid access', () => {
    expect(reusableAccess(null)).toBeNull()
    expect(reusableAccess({ connected: true, enabled: true, terminals: false })).toBeNull()
  })
})
