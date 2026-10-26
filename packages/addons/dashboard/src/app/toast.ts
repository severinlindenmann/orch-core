import { toast } from 'sonner'
import { ApiError } from '@/api/types'

/** The one way a failed request reaches the user: the API message, with its hint as the description. */
export const ERROR_TOAST_ID = 'api-error'

export function toastApiError(err: unknown, fallback = 'Something went wrong.', id?: string | number) {
  // A failure stays until the person dismisses it, but there is at most one: a new one replaces the older, so
  // confirmations and Undo always have a visible slot. (Replacing a loading toast by `id` drops the older error too.)
  if (id !== undefined) toast.dismiss(ERROR_TOAST_ID)
  const opts = { id: id ?? ERROR_TOAST_ID, duration: Infinity }
  if (err instanceof ApiError) toast.error(err.message, { ...opts, description: err.hint })
  else toast.error(fallback, { ...opts, description: undefined })
}
