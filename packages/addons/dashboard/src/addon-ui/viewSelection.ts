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

const emptyTrees = new Set<string>()
/** A tree says whether it has nothing to list (a `search` aimed at it then hides: there is nothing to search). */
export function setTreeEmpty(key: string, empty: boolean) {
  if (empty === emptyTrees.has(key)) return
  if (empty) emptyTrees.add(key)
  else emptyTrees.delete(key)
  emit()
}
export const useTreeEmpty = (key: string | null): boolean => useSyncExternalStore(subscribe, () => (key !== null && emptyTrees.has(key)))

const searching = new Set<string>()
/** A `search` aimed at a tree says it has a query open: on a phone the tree gives way to the hits (no duplicate list). */
export function setSearching(key: string, on: boolean) {
  if (on === searching.has(key)) return
  if (on) searching.add(key)
  else searching.delete(key)
  emit()
}
export const useSearching = (key: string): boolean => useSyncExternalStore(subscribe, () => searching.has(key))

export function useSelection(key: string): [string | null, (id: string | null) => void] {
  const value = useSyncExternalStore(subscribe, () => chosen.get(key) ?? null)
  return [value, useCallback((id: string | null) => setSelection(key, id), [key])]
}

/** Tests only. */
export function resetSelections() {
  chosen.clear()
  emptyTrees.clear()
  searching.clear()
  emit()
}
