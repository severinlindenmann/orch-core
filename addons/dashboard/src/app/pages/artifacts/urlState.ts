import { useCallback, useEffect, useRef } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { toast } from 'sonner'
import type { ArtifactsSearch } from '@/app/search'
import type { ArtifactItem } from '@/api/types'

/** An artifact's id in the address: its ticket key and the start of its content hash (no file names or titles). */
export const artifactUrlId = (a: Pick<ArtifactItem, 'ticket' | 'sha256'>) => `${a.ticket}.${a.sha256.slice(0, 12)}`

/**
 * `/w/DEMO/artifacts?view=grid&a=DEMO-0043.3f2a…`: the layout and the shown artifact live in the address. The page
 * keeps its own state; this hook reads the address once the list is there and writes the address back on changes.
 */
export function useArtifactsUrl() {
  const search = useSearch({ strict: false }) as ArtifactsSearch
  const navigate = useNavigate()
  const latest = useRef(search)
  latest.current = search
  const set = useCallback(
    (patch: Partial<ArtifactsSearch>) =>
      void navigate({
        to: '/artifacts',
        search: Object.fromEntries(Object.entries({ ...latest.current, ...patch }).filter(([, v]) => v !== undefined)) as ArtifactsSearch,
        replace: true,
      }),
    [navigate],
  )

  const pending = useRef(search.a)
  return { view: search.view, setView: (view: 'list' | 'grid') => set({ view }), a: search.a, set, pending }
}

/** Opens the artifact an `a` from the address names once the list is there; after that the address follows the page. */
export function useShownArtifactInUrl(url: ReturnType<typeof useArtifactsUrl>, { items, shown, show }: {
  items: ArtifactItem[] | undefined
  /** The artifact the page shows (selected row or open drawer). */
  shown: ArtifactItem | null
  /** Shows an artifact the address names, as a click on it would. */
  show: (a: ArtifactItem) => void
}) {
  const { pending, set, a } = url
  // An `a` from the address waits for the list, then opens that artifact (or says it is not in this list); after
  // that the address follows what the page shows.
  const shownId = shown ? artifactUrlId(shown) : undefined
  useEffect(() => {
    const want = pending.current
    if (want) {
      if (!items) return
      pending.current = undefined
      const hit = items.find((x) => artifactUrlId(x) === want)
      if (hit) return show(hit)
      toast('The linked artifact is not in this list', { description: 'It may be on another page, filtered out, or not visible to you.' })
    }
    if (shownId !== a) set({ a: shownId })
  }, [items, show, shownId, a, set, pending])
}
