import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requestSettingsFocus, takeSettingsFocus } from './AddonSettingsPanel'

// F1 N2: a focus request for a row's Settings button that no row takes lapses after 2 s, so it cannot steal focus
// on a later visit.
beforeEach(() => vi.useFakeTimers())
afterEach(() => {
  requestSettingsFocus(null)
  vi.useRealTimers()
})

describe('requestSettingsFocus', () => {
  it('is taken once by the row it names, within 2 s', () => {
    requestSettingsFocus('usage')
    vi.advanceTimersByTime(1999)
    expect(takeSettingsFocus('wiki')).toBe(false)
    expect(takeSettingsFocus('usage')).toBe(true)
    expect(takeSettingsFocus('usage')).toBe(false) // once
  })

  it('lapses after 2 s', () => {
    requestSettingsFocus('usage')
    vi.advanceTimersByTime(2000)
    expect(takeSettingsFocus('usage')).toBe(false)
  })

  it('a newer request gets its own 2 s: the older one\'s lapse does not cancel it', () => {
    requestSettingsFocus('usage')
    vi.advanceTimersByTime(1500)
    requestSettingsFocus('usage')
    vi.advanceTimersByTime(600) // the first request's 2 s are over, the second's are not
    expect(takeSettingsFocus('usage')).toBe(true)
  })

  it('another addon or null replaces the request', () => {
    requestSettingsFocus('usage')
    requestSettingsFocus('wiki')
    expect(takeSettingsFocus('usage')).toBe(false)
    vi.advanceTimersByTime(2000)
    expect(takeSettingsFocus('wiki')).toBe(false)
    requestSettingsFocus('wiki')
    requestSettingsFocus(null)
    expect(takeSettingsFocus('wiki')).toBe(false)
  })
})
