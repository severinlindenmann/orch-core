import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { validateBoardSearch, type BoardSearch } from '@/app/search'
import { NO_FILTERS, type Filters } from './lib'
import type { View } from './Toolbar'

/** Board search params -> the toolbar's filters (absent: "all"). */
export function filtersOf(s: BoardSearch): Filters {
  return { mine: !!s.mine, type: s.type ?? 'all', label: s.label ?? 'all', person: s.person ?? 'all', epic: s.epic ?? 'all', q: s.q ?? '' }
}

/** The toolbar's filters and view -> board search params (defaults left out, so a plain board is plain `/board`). */
export function searchOf(f: Filters, view: View): BoardSearch {
  const out: BoardSearch = {}
  if (view !== 'board') out.view = view
  if (f.mine) out.mine = true
  for (const k of ['type', 'label', 'person', 'epic'] as const) if (f[k] !== NO_FILTERS[k]) out[k] = f[k]
  if (f.q.trim()) out.q = f.q.trim()
  return out
}

/**
 * View and filters live in the address (`/w/DEMO/board?view=list&mine=true&q=tariff`): Back restores them and a
 * copied link opens the same board. The search text is local while typing and reaches the address after 200 ms.
 */
export function useBoardUrlState() {
  // Read through the route's validator again: a parent match passes the raw params on.
  const search = validateBoardSearch(useSearch({ strict: false }))
  const navigate = useNavigate()
  const view: View = search.view ?? 'board'
  const fromUrl = filtersOf(search)
  const [q, setQ] = useState(fromUrl.q)
  const sentQ = useRef(fromUrl.q)
  // Back/Forward or a pasted link brings another search text.
  useEffect(() => {
    if (fromUrl.q !== sentQ.current) {
      sentQ.current = fromUrl.q
      setQ(fromUrl.q)
    }
  }, [fromUrl.q])

  const go = useCallback(
    (f: Filters, v: View, replace = false) => void navigate({ to: '/board', search: searchOf(f, v), replace }),
    [navigate],
  )
  const filters: Filters = { ...fromUrl, q }
  const rest = JSON.stringify({ ...fromUrl, q: '' })
  useEffect(() => {
    if (q === sentQ.current) return
    const id = setTimeout(() => {
      sentQ.current = q
      go({ ...(JSON.parse(rest) as Filters), q }, view, true)
    }, 200)
    return () => clearTimeout(id)
  }, [q, rest, view, go])

  const setFilters = (f: Filters) => {
    setQ(f.q)
    if (f.q !== q && JSON.stringify({ ...f, q: '' }) === JSON.stringify({ ...filters, q: '' })) return // typing only
    sentQ.current = f.q
    go(f, view)
  }
  const setView = (v: View) => go(filters, v)
  return { view, setView, filters, setFilters }
}
