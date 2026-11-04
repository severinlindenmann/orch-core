import { useSyncExternalStore } from 'react'

// The ticket that was just created, remembered for a moment so the list can point at it (a short highlight and a "New"
// tag). Module level: the creating sheet and the list are different components; nothing is stored or sent anywhere.
export const JUST_CREATED_MS = 3000

let current: string | null = null
let timer: ReturnType<typeof setTimeout> | undefined
const subs = new Set<() => void>()
const emit = () => subs.forEach((f) => f())

export function markJustCreated(key: string): void {
  current = key
  clearTimeout(timer)
  timer = setTimeout(() => { current = null; emit() }, JUST_CREATED_MS)
  emit()
}

/** The key created in the last few seconds, or null. */
export function useJustCreated(): string | null {
  return useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f) } }, () => current, () => null)
}
