import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { queries } from '@/api/queries'

/**
 * A page's "could not load" state (G4 review M5): the viewer or the workspaces failed, or one of the page's own
 * queries did, with no data to show. Retry asks for all of them again. Without this, a page gated on them would show
 * its skeleton for good.
 */
export function useLoadFailure(...own: Pick<UseQueryResult, 'isError' | 'data' | 'refetch'>[]): { failed: boolean; retry: () => void } {
  const me = useQuery(queries.me())
  const workspaces = useQuery(queries.workspaces())
  const all = [me, workspaces, ...own]
  return {
    failed: all.some((q) => q.isError && q.data === undefined),
    retry: () => {
      for (const q of all) if (q.isError) void q.refetch()
    },
  }
}

/**
 * For a page's first screen only: true while `waiting` holds, for at most `ms` after mount. Once it has been false
 * (or the time ran out) it stays false, so a part that reloads later never sends the page back to its skeleton.
 */
export function useWaitAtMost(waiting: boolean, ms: number): boolean {
  const [done, setDone] = useState(false)
  useEffect(() => {
    if (done) return
    if (!waiting) return setDone(true)
    const t = setTimeout(() => setDone(true), ms)
    return () => clearTimeout(t)
  }, [waiting, done, ms])
  return waiting && !done
}
