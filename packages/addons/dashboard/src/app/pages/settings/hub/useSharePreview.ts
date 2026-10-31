import { useEffect, useState } from 'react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import type { HubPreview, HubRule } from '@/api/hub'

/** The host's words when a rule reaches more people than one review can show (core `shareops.TOO_MANY`). */
export const TOO_MANY = 'This rule reaches too many people. Narrow it to share.'

/** What the host's refusal always contains (it may be prefixed by the relay); the page matches this, not the whole sentence. */
const TOO_MANY_MARK = 'reaches too many people'

export type ShareChange = { add?: never; remove?: never } | { add: HubRule; remove?: never } | { remove: HubRule; add?: never }

/**
 * The preview of ONE share change (`share.preview`), asked 300 ms after the last edit. `current` is the answer to exactly
 * the change given (an older answer is never shown for a newer change); its `plan` is what a confirm binds. One place
 * for Settings and the ticket's own Share dialog, so both show and bind the same review.
 */
export function useSharePreview(ws: string, change: ShareChange | undefined) {
  const encoded = change ? JSON.stringify(change) : ''
  const [preview, setPreview] = useState<{ key: string; data: HubPreview }>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let alive = true
    setError('')
    if (!encoded) return
    const timer = setTimeout(() => {
      void api.getHubPreview(ws, JSON.parse(encoded) as ShareChange).then(
        data => { if (alive) setPreview({ key: encoded, data }) },
        err => { if (alive) setError(err instanceof ApiError && err.message.includes(TOO_MANY_MARK) ? TOO_MANY : 'Could not preview this rule. Check its value or try again.') },
      )
    }, 300)
    return () => { alive = false; clearTimeout(timer) }
  }, [ws, encoded, retry])
  return { current: preview?.key === encoded ? preview.data : undefined, error, retry: () => setRetry(n => n + 1) }
}
