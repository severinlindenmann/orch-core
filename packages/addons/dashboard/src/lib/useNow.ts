import { useSyncExternalStore } from 'react'
import { nowMs } from '@/lib/time'

// One shared clock for every "N min ago" on a page: a single interval, only while something is mounted (P15).
const TICK_MS = 30_000
const listeners = new Set<() => void>()
let timer: ReturnType<typeof setInterval> | null = null
let current = 0

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  current = nowMs()
  if (!timer) timer = setInterval(() => { current = nowMs(); listeners.forEach((l) => l()) }, TICK_MS)
  return () => {
    listeners.delete(fn)
    if (listeners.size === 0 && timer) { clearInterval(timer); timer = null }
  }
}
const snapshot = () => current || (current = nowMs())

/** The host's now, moved on every 30 s by one shared timer. */
export function useNow(): number {
  return useSyncExternalStore(subscribe, snapshot, snapshot)
}
