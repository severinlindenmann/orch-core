import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', () => ({ toast: { error: vi.fn(), dismiss: vi.fn() } }))

import { toast } from 'sonner'
import { ApiError } from '@/api/types'
import { toastApiError } from './toast'

describe('toastApiError', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the API message with the hint as description', () => {
    toastApiError(new ApiError(409, { code: 'human_only', message: 'Done is reached by a verdict', hint: 'Give the verdict on the ticket page', retryable: false }))
    expect(toast.error).toHaveBeenCalledWith('Done is reached by a verdict', { id: 'api-error', duration: Infinity, description: 'Give the verdict on the ticket page' })
  })

  it('omits the description without a hint', () => {
    toastApiError(new ApiError(403, { code: 'forbidden', message: 'Nope', retryable: false }))
    expect(toast.error).toHaveBeenCalledWith('Nope', { id: 'api-error', duration: Infinity, description: undefined })
  })

  it('uses a generic message for anything else', () => {
    toastApiError(new Error('boom'))
    expect(toast.error).toHaveBeenCalledWith('Something went wrong.', { id: 'api-error', duration: Infinity, description: undefined })
  })

  it('uses a caller fallback for non-API errors', () => {
    toastApiError('x', 'Could not rename')
    expect(toast.error).toHaveBeenCalledWith('Could not rename', { id: 'api-error', duration: Infinity, description: undefined })
  })

  it('keeps one error toast: a new error reuses the same id, so it replaces the older', () => {
    toastApiError(new Error('a'))
    toastApiError(new Error('b'))
    const ids = vi.mocked(toast.error).mock.calls.map((c) => (c[1] as { id?: string }).id)
    expect(ids).toEqual(['api-error', 'api-error'])
  })
})
