import { toast } from 'sonner'
import { ApiError } from '@/api/types'

/** The one way a failed request reaches the user: the API message, with its hint as the description. */
export function toastApiError(err: unknown, fallback = 'Something went wrong.', id?: string | number) {
  // A failure stays until the person dismisses it: it is the only record of what went wrong.
  const opts = id === undefined ? { duration: Infinity } : { id, duration: Infinity }
  if (err instanceof ApiError) toast.error(err.message, { ...opts, description: err.hint })
  else toast.error(fallback, { ...opts, description: undefined })
}
