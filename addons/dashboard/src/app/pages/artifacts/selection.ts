import { useCallback, useEffect, useRef, useState } from 'react'
import type { ArtifactItem } from '@/api/types'

export type View = 'list' | 'grid'

/** A listed artifact's identity (a ticket may hold two files of one name with different content). */
export const artifactKey = (a: ArtifactItem) => `${a.ticket}/${a.name}/${a.sha256}`

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

/**
 * The Artifacts page's view and preview state, in one place (DECISIONS-LOG, G3). `current` is the artifact being
 * previewed (null: nothing is); the page shows it in the pane or the drawer by width, so resizing moves the same item
 * between them. The identity survives list ↔ grid; a new workspace, filter or page (`context`) clears it, and so does a
 * settled list that no longer holds it. URL state (`?view=`, `?a=`) can be wired to `view` / `currentKey` here.
 */
export function useArtifactSelection({ person, items, settled, context }: { person?: string; items?: ArtifactItem[]; settled: boolean; context: string }) {
  const [view, setView] = useState<View | null>(null)
  const [currentKey, setCurrentKey] = useState<string | null>(null)
  /** The item last previewed: Close returns focus to its Preview button. */
  const lastKey = useRef<string | null>(null)

  // The list/grid choice is remembered per person; nothing is read until the viewer is known.
  useEffect(() => {
    if (person) setView(readView(person))
  }, [person])
  const chooseView = useCallback(
    (v: View) => {
      setView(v)
      try {
        if (person) localStorage.setItem(viewKey(person), v)
      } catch {
        /* storage unavailable: the choice lasts for this page only */
      }
    },
    [person],
  )

  // Another workspace, filter or page: the preview belonged to the old results.
  const seen = useRef(context)
  useEffect(() => {
    if (seen.current === context) return
    seen.current = context
    setCurrentKey(null)
  }, [context])
  // Settled results without it (removed, or no longer visible to this viewer): nothing stale stays open.
  useEffect(() => {
    if (settled && currentKey && items && !items.some((a) => artifactKey(a) === currentKey)) setCurrentKey(null)
  }, [settled, items, currentKey])

  const current = (currentKey && items?.find((a) => artifactKey(a) === currentKey)) || null
  const preview = useCallback((a: ArtifactItem) => {
    lastKey.current = artifactKey(a)
    setCurrentKey(artifactKey(a))
  }, [])
  const close = useCallback(() => setCurrentKey(null), [])
  return { view, chooseView, current, currentKey, preview, close, lastKey }
}
