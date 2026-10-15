// Mock only: Reset and a dataset switch start Today's queue afresh. One counter (Today keys its queue on it) and the
// "seen" lists cleared, so nothing is marked new and no item waits behind the pill. Browser storage may be blocked
// (private window, sandboxed viewer): clearing then does nothing, and the counter still moves.
import { useSyncExternalStore } from 'react'

let generation = 0
const listeners = new Set<() => void>()

/** The prefix of Today's per-workspace, per-person "seen" lists in localStorage. */
export const SEEN_PREFIX = 'orch.today.seen.'

export function restartToday() {
  try {
    for (const key of Object.keys(localStorage)) if (key.startsWith(SEEN_PREFIX)) localStorage.removeItem(key)
  } catch {
    /* storage unavailable: there is no seen list to clear */
  }
  generation += 1
  for (const l of listeners) l()
}

/** Changes every time `restartToday` runs. */
export function useTodayGeneration(): number {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l)
      return () => listeners.delete(l)
    },
    () => generation,
  )
}
