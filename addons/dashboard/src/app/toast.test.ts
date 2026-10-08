import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))

import { toast } from 'sonner'
import { ApiError } from '@/api/types'
import { toastApiError } from './toast'

describe('toastApiError', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows the API message with the hint as description', () => {
    toastApiError(new ApiError(409, { code: 'human_only', message: 'Done is reached by a verdict', hint: 'Give the verdict on the ticket page', retryable: false }))
    expect(toast.error).toHaveBeenCalledWith('Done is reached by a verdict', { description: 'Give the verdict on the ticket page' })
  })

  it('omits the description without a hint', () => {
    toastApiError(new ApiError(403, { code: 'forbidden', message: 'Nope', retryable: false }))
    expect(toast.error).toHaveBeenCalledWith('Nope', { description: undefined })
  })

  it('uses a generic message for anything else', () => {
    toastApiError(new Error('boom'))
    expect(toast.error).toHaveBeenCalledWith('Something went wrong.', { description: undefined })
  })

  it('uses a caller fallback for non-API errors', () => {
    toastApiError('x', 'Could not rename')
    expect(toast.error).toHaveBeenCalledWith('Could not rename', { description: undefined })
  })
})
