import { describe, expect, it } from 'vitest'
import { ApiError, SIGNING_METHOD_STALE, signingMethodStale } from '@/api/types'

describe('the page learns that the click-to-sign switch moved', () => {
  it('only the refusals that mean the switch moved raise the event', () => {
    let seen = 0
    const on = () => { seen += 1 }
    window.addEventListener(SIGNING_METHOD_STALE, on)
    new ApiError(409, { code: 'sign.click_only', message: 'x', retryable: false })
    new ApiError(409, { code: 'human.click_refused', message: 'x', retryable: false, reason: 'disabled' })
    new ApiError(409, { code: 'sign.convenience_unavailable', message: 'x', retryable: false, reason: 'disabled' })
    expect(seen).toBe(3)
    new ApiError(409, { code: 'sign.convenience_unavailable', message: 'x', retryable: false, reason: 'relay_paired' })
    new ApiError(404, { code: 'not_found', message: 'x', retryable: false })
    expect(seen).toBe(3)
    window.removeEventListener(SIGNING_METHOD_STALE, on)
    expect(signingMethodStale({ code: 'sign.click_only' })).toBe(true)
  })
})
