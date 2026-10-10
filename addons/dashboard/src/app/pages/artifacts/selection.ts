import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { toast } from 'sonner'
import type { ArtifactItem } from '@/api/types'
import { validateArtifactsSearch, type ArtifactsSearch } from '@/app/search'

export type View = 'list' | 'grid'

/**
 * A listed artifact's identity in the page: `<ticket>/<name>/<sha256>` (a ticket may hold two files of one name with
 * different content). The address carries the shorter `artifactUrlId`.
 */
export const artifactKey = (a: Pick<ArtifactItem, 'ticket' | 'name' | 'sha256'>) => `${a.ticket}/${a.name}/${a.sha256}`
/** The parts of an `artifactKey` (names may contain `/`: the ticket is the first segment, the hash the last). */
export function parseArtifactKey(key: string): { ticket: string; name: string; sha256: string } | null {
  const first = key.indexOf('/')
  const last = key.lastIndexOf('/')
  if (first <= 0 || last <= first + 1 || last === key.length - 1) return null
  return { ticket: key.slice(0, first), name: key.slice(first + 1, last), sha256: key.slice(last + 1) }
}
/** An artifact's id in the address (`?a=`): its ticket key and the start of its content hash (no file names or titles). */
export const artifactUrlId = (a: Pick<ArtifactItem, 'ticket' | 'sha256'>) => `${a.ticket}.${a.sha256.slice(0, 12)}`

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
  /** The viewer could not be read: without `?view=` the page shows the list instead of waiting for good. */
  viewerFailed?: boolean
}

/**
 * The Artifacts page's view and shown artifact (DECISIONS-LOG G2, G3). The address is the one source:
 * `/w/DEMO/artifacts?view=grid&a=DEMO-0043.3f2a…`. `?view=` wins; without it the person's remembered choice
 * (localStorage) applies, else list. Choosing a view pushes a history entry (Back returns to the other one); showing
 * or closing an artifact replaces the entry. `current` is the artifact `?a=` names in the results (null: none); the
 * page shows it in the pane or the drawer by width, so resizing moves it between them and list ↔ grid keeps it. Back
 * and Forward restore both. A new workspace, filter or page clears `?a=`, and so do settled results without it (a
 * deep link to another page, a filtered-out or no longer visible artifact: fails closed, with a toast for a link
 * that never showed); every clearing replaces.
 */
export function useArtifactSelection({ person, items, settled, context, viewerFailed = false }: ArtifactSelectionOptions) {
  // Read through the route's validator again: a parent match passes the raw params on.
  const search = validateArtifactsSearch(useSearch({ strict: false }))
  const navigate = useNavigate()
  const write = useCallback(
    (patch: Partial<ArtifactsSearch>, replace: boolean) =>
      void navigate({
        to: '/artifacts',
        search: (prev: Record<string, unknown>) => Object.fromEntries(Object.entries({ ...prev, ...patch }).filter(([, v]) => v !== undefined)) as ArtifactsSearch,
        replace,
      }),
    [navigate],
  )

  // The remembered layout, read once the viewer is known: only the default when the address has no `?view=`.
  const [remembered, setRemembered] = useState<View | null>(null)
  useEffect(() => {
    if (person) setRemembered(readView(person))
  }, [person])
  const view: View | null = search.view ?? remembered ?? (viewerFailed ? 'list' : null)
  const chooseView = useCallback(
    (v: View) => {
      // Remembered for the next visit only: an entry without `?view=` keeps the default it loaded with, so Back
      // returns to it.
      try {
        if (person) localStorage.setItem(viewKey(person), v)
      } catch {
        /* storage unavailable: the choice lasts for this page only */
      }
      write({ view: v }, false)
    },
    [person, write],
  )

  const want = search.a
  /** The item last shown: Close returns focus to its Preview button. */
  const lastKey = useRef<string | null>(null)
  // Two files with the same content on one ticket share an address id: the one last chosen wins, else the first.
  const matches = want ? (items ?? []).filter((a) => artifactUrlId(a) === want) : []
  const current = matches.find((a) => artifactKey(a) === lastKey.current) ?? matches[0] ?? null
  const currentKey = current ? artifactKey(current) : null
  useEffect(() => {
    if (currentKey) lastKey.current = currentKey
  }, [currentKey])

  const setCurrentKey = useCallback(
    (key: string | null) => {
      const parts = key ? parseArtifactKey(key) : null
      if (key) lastKey.current = key
      write({ a: parts ? artifactUrlId(parts) : undefined }, true)
    },
    [write],
  )

  // Another workspace, filter or page: the shown artifact belonged to the old results. Counted from the first
  // settled results, so a cold load's own setup (workspace arriving, filters reset) never clears a deep link.
  const seen = useRef<string | null>(null)
  useEffect(() => {
    if (!settled && seen.current === null) return
    if (seen.current === null || seen.current === context) {
      seen.current = context
      return
    }
    seen.current = context
    if (want) write({ a: undefined }, true)
  }, [context, settled, want, write])
  // Settled results without it: fail closed. Only the link the page was opened with says why (a clearing the page
  // itself caused, such as a new search, stays quiet).
  const linked = useRef(search.a)
  if (current && linked.current === want) linked.current = undefined
  useEffect(() => {
    if (!settled || !want || !items || items.some((a) => artifactUrlId(a) === want)) return
    if (linked.current === want) toast('The linked artifact is not in this list', { description: 'It may be on another page, filtered out, or not visible to you.' })
    write({ a: undefined }, true)
  }, [settled, items, want, write])

  const preview = useCallback((a: ArtifactItem) => setCurrentKey(artifactKey(a)), [setCurrentKey])
  const close = useCallback(() => setCurrentKey(null), [setCurrentKey])
  return { view, chooseView, current, currentKey, setCurrentKey, preview, close, lastKey }
}
