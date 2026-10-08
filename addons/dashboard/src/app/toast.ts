import { toast } from 'sonner'
import { ApiError } from '@/api/types'

/** The one way a failed request reaches the user: the API message, with its hint as the description. */
export function toastApiError(err: unknown, fallback = 'Something went wrong.', id?: string | number) {
  const opts = id === undefined ? {} : { id }
  if (err instanceof ApiError) toast.error(err.message, { ...opts, description: err.hint })
  else toast.error(fallback, { ...opts, description: undefined })
}
