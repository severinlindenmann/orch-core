// Live updates. The mock polls its cursor and refreshes the data queries when it moves. A real host streams scoped
// items (api/useLiveUpdates.ts, the design's invalidation table); an open ticket that is no longer visible is closed.
import { useQueryClient } from '@tanstack/react-query'
import { useRouter } from '@tanstack/react-router'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { isRealHost } from '@/api/features'
import { useSigningMethodRefresh } from '@/api/signingRefresh'
import { useLiveUpdates as useStream } from '@/api/useLiveUpdates'
import { useWorkspace } from './workspace'

const POLL_MS = 2000
const LIVE_KEYS = ['today', 'tickets', 'ticket', 'agents', 'agent-activity', 'grants', 'addon-state', 'addon-decisions', 'workspaces', 'add-ons', 'quick', 'artifacts', 'relay', 'identity']

function usePollUpdates() {
  const qc = useQueryClient()
  const wsId = useWorkspace().workspace?.id
  useEffect(() => {
    // Tests do not need the poll; it only adds refetch/re-render churn under parallel load.
    if (!wsId || import.meta.env.MODE === 'test') return
    let last: number | undefined
    let stopped = false
    const tick = async () => {
      if (document.visibilityState !== 'visible') return
      try {
        const { cursor } = await api.getCursor(wsId)
        if (stopped) return
        if (last !== undefined && cursor !== last) for (const k of LIVE_KEYS) void qc.invalidateQueries({ queryKey: [k] })
        last = cursor
      } catch {
        // transient; try again on the next tick
      }
    }
    void tick()
    const timer = setInterval(() => void tick(), POLL_MS)
    return () => {
      stopped = true
      clearInterval(timer)
    }
  }, [qc, wsId])
}

/** The open ticket page whose key is in `keys` (or, for a workspace-wide loss, any ticket page) goes back to the board. */
export function useCloseTicketView() {
  const router = useRouter()
  return useCallback(
    (_uid?: string, keys?: string[]) => {
      const m = /^\/ticket\/([^/]+)\/?$/.exec(router.state.location.pathname)
      const open = m && decodeURIComponent(m[1])
      if (!open || (keys && !keys.includes(open))) return
      toast(`${open} is no longer visible to you`)
      void router.navigate({ to: '/board', replace: true })
    },
    [router],
  )
}

function useStreamUpdates() {
  const ws = useWorkspace().workspace?.id
  const close = useCloseTicketView()
  const { revoked } = useStream(ws, close)
  // The caches are already purged; say why the pages are empty instead of leaving host errors to explain it.
  useEffect(() => {
    if (!revoked) return
    toast.error('Workspace access is no longer available.', { id: 'workspace-revoked', duration: Infinity })
    return () => { toast.dismiss('workspace-revoked') }
  }, [revoked])
}

// The host kind is read once, at mount, so the same hook runs on every render (the dev layout preview switches the
// features of a mounted app; production sets them before the router exists).
export function useLiveUpdates() {
  useSigningMethodRefresh()
  const [real] = useState(isRealHost)
  return (real ? useStreamUpdates : usePollUpdates)()
}
