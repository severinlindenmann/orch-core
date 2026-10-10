import { useCallback, useEffect, useRef, useState } from 'react'
import type { ArtifactItem } from '@/api/types'

export type View = 'list' | 'grid'

/**
 * A listed artifact's identity: `<ticket>/<name>/<sha256>` (a ticket may hold two files of one name with different
 * content). URL wiring maps its own form (e.g. `?a=<KEY>.<sha256[:12]>`) to and from this with `parseArtifactKey`.
 */
export const artifactKey = (a: Pick<ArtifactItem, 'ticket' | 'name' | 'sha256'>) => `${a.ticket}/${a.name}/${a.sha256}`
/** The parts of an `artifactKey` (names may contain `/`: the ticket is the first segment, the hash the last). */
export function parseArtifactKey(key: string): { ticket: string; name: string; sha256: string } | null {
  const first = key.indexOf('/')
  const last = key.lastIndexOf('/')
  if (first <= 0 || last <= first + 1 || last === key.length - 1) return null
  return { ticket: key.slice(0, first), name: key.slice(first + 1, last), sha256: key.slice(last + 1) }
}

const viewKey = (person: string) => `orch.artifacts.view.${person}`
function readView(person: string): View {
  try {
    return localStorage.getItem(viewKey(person)) === 'grid' ? 'grid' : 'list'
  } catch {
    return 'list'
  }
}

/** The item's Preview button, or the results heading when the item is gone (focus never falls to the page body). */
export function previewTarget(key: string | null): HTMLElement | null {
  const button = key ? document.querySelector<HTMLElement>(`[data-artifact="${CSS.escape(key)}"] [data-preview]`) : null
  return button ?? document.getElementById('artifact-results')
}

export interface ArtifactSelectionOptions {
  person?: string
  items?: ArtifactItem[]
  /** The results are the ones for the current context (not a placeholder from the previous query). */
  settled: boolean
  /** Workspace, filters and page: a change clears the preview. */
  context: string
  /** Controlled view (e.g. from `?view=`). Uncontrolled: the person's remembered choice (localStorage), else list. */
  view?: View | null
  onViewChange?: (v: View) => void
  /** Controlled preview (e.g. from `?a=`): an `artifactKey` or null. */
  currentKey?: string | null
  onCurrentKeyChange?: (key: string | null) => void
}

/**
 * The Artifacts page's view and preview state, in one place (DECISIONS-LOG, G3). `current` is the artifact being
 * previewed (null: nothing is); the page shows it in the pane or the drawer by width, so resizing moves the same item
 * between them. The identity survives list ↔ grid; a new context clears it, and so does a settled list that no longer
 * holds it. Both values can be controlled (value + onChange, like an input); every change, including the clearing,
 * goes through the change callback, so a URL can be the one writer. localStorage is only the uncontrolled default.
 */
export function useArtifactSelection(o: ArtifactSelectionOptions) {
  const { person, items, settled, context } = o
  const [ownView, setOwnView] = useState<View | null>(null)
  const [ownKey, setOwnKey] = useState<string | null>(null)
  const viewControlled = o.view !== undefined
  const keyControlled = o.currentKey !== undefined
  const view = viewControlled ? o.view! : ownView
  const currentKey = keyControlled ? o.currentKey! : ownKey
  /** The item last previewed: Close returns focus to its Preview button. */
  const lastKey = useRef<string | null>(null)
  useEffect(() => {
    if (currentKey) lastKey.current = currentKey
  }, [currentKey])

  // Uncontrolled: the person's remembered choice, read once the viewer is known (never over a choice made meanwhile).
  useEffect(() => {
    if (person && !viewControlled) setOwnView((v) => v ?? readView(person))
  }, [person, viewControlled])

  const onKey = o.onCurrentKeyChange
  const setCurrentKey = useCallback(
    (key: string | null) => {
      if (key) lastKey.current = key
      if (!keyControlled) setOwnKey(key)
      onKey?.(key)
    },
    [keyControlled, onKey],
  )
  const onView = o.onViewChange
  const chooseView = useCallback(
    (v: View) => {
      if (!viewControlled) setOwnView(v)
      try {
        if (person) localStorage.setItem(viewKey(person), v)
      } catch {
        /* storage unavailable: the choice lasts for this page only */
      }
      onView?.(v)
    },
    [person, viewControlled, onView],
  )

  // Another workspace, filter or page: the preview belonged to the old results.
  const seen = useRef(context)
  useEffect(() => {
    if (seen.current === context) return
    seen.current = context
    if (currentKey) setCurrentKey(null)
  }, [context, currentKey, setCurrentKey])
  // Settled results without it (removed, on another page, or no longer visible to this viewer): fail closed.
  useEffect(() => {
    if (settled && currentKey && items && !items.some((a) => artifactKey(a) === currentKey)) setCurrentKey(null)
  }, [settled, items, currentKey, setCurrentKey])

  const current = (currentKey && items?.find((a) => artifactKey(a) === currentKey)) || null
  const preview = useCallback((a: ArtifactItem) => setCurrentKey(artifactKey(a)), [setCurrentKey])
  const close = useCallback(() => setCurrentKey(null), [setCurrentKey])
  return { view, chooseView, current, currentKey, setCurrentKey, preview, close, lastKey }
}
