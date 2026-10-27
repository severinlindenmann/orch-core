// The item a viewer has open in a `tree`, kept per workspace, add-on and tree id for this browser tab only (never sent to
// the add-on). A `search` node with `target` opens its result in that tree through the same store.
import { useCallback, useSyncExternalStore } from 'react'

const chosen = new Map<string, string>()
const listeners = new Set<() => void>()
const emit = () => listeners.forEach((l) => l())
const subscribe = (l: () => void) => {
  listeners.add(l)
  return () => void listeners.delete(l)
}

export const selectionKey = (ws: string | undefined, addon: string, tree: string) => `${ws ?? ''}/${addon}/${tree}`

export function setSelection(key: string, id: string | null) {
  if (id === null) chosen.delete(key)
  else chosen.set(key, id)
  emit()
}

export function useSelection(key: string): [string | null, (id: string | null) => void] {
  const value = useSyncExternalStore(subscribe, () => chosen.get(key) ?? null)
  return [value, useCallback((id: string | null) => setSelection(key, id), [key])]
}

/** Tests only. */
export function resetSelections() {
  chosen.clear()
  emit()
}
