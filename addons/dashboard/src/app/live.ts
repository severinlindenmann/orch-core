// Live updates: poll the workspace cursor and refresh the data queries when it moves.
import { useQueryClient } from '@tanstack/react-query'
import { useEffect } from 'react'
import { api } from '@/api/client'
import { useWorkspace } from './workspace'

const POLL_MS = 2000
const LIVE_KEYS = ['today', 'tickets', 'ticket', 'agents', 'addon-state', 'workspaces']

export function useLiveUpdates() {
  const qc = useQueryClient()
  const wsId = useWorkspace().workspace?.id
  useEffect(() => {
    if (!wsId) return
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
