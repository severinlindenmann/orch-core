import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, LayoutGrid, List, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { ArtifactItem, ArtifactQuery } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { usePageHeader } from '../../shell/ShellUi'
import { useWorkspace } from '../../workspace'
import { FilterSelect } from '../board/Toolbar'
import { SearchBox } from '../tickets/Filters'
import { agentName, displayName } from '../ticket/shared'
import { useElementWidth } from '@/lib/useElementWidth'
import { cn } from '@/lib/utils'
import { useMediaQuery, WIDE_QUERY } from '../today/shared'
import { ArtifactPane, ArtifactPreview, openMode } from './Preview'
import { ArtifactGrid, ArtifactList, artifactKey } from './views'
import { useArtifactsUrl, useShownArtifactInUrl } from './urlState'

type View = 'list' | 'grid'
const viewKey = (person: string) => `orch.artifacts.view.${person}`
function readView(person: string): View {
  try {
    return localStorage.getItem(viewKey(person)) === 'grid' ? 'grid' : 'list'
  } catch {
    return 'list'
  }
}

/**
 * The list view previews beside the table on a window of at least 1280 px (the Today rule) whose page area still has
 * SPLIT_MIN_PAGE px (not beside a wide terminal dock); otherwise the drawer opens (owner feedback E, DECISIONS-LOG F2).
 */
export const SPLIT_MIN_PAGE = 960

/**
 * j/k are for the list: not while typing, not with a modifier, not when another handler took the key, and not while
 * any dialog other than this page's own artifact drawer is open (the shell's keyboardBusy rule, with that one
 * exception so the drawer follows the selection).
 */
const busy = (e: KeyboardEvent) => {
  if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return true
  const t = e.target as HTMLElement | null
  if (t?.isContentEditable || t?.closest('input, textarea, select, [role="combobox"], [role="listbox"], [role="menu"]')) return true
  return [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')].some((d) => !d.hasAttribute('data-artifact-drawer'))
}

const SINCE = [
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
]

/** /artifacts: every artifact of the tickets the viewer can see in this workspace (the host filters and pages). */
export function ArtifactsPage() {
  usePageHeader('Artifacts')
  const { workspace } = useWorkspace()
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const ws = workspace?.id
  const person = me.data?.person
  const [filters, setFilters] = useState<Omit<ArtifactQuery, 'page' | 'per'>>({})
  const [qInput, setQInput] = useState('')
  const [page, setPage] = useState(1)
  const [view, setView] = useState<View | null>(null)
  const url = useArtifactsUrl()
  const [open, setOpen] = useState<ArtifactItem | null>(null)
  // The list view's selected row: the pane (or an open drawer) shows it; j/k move it.
  const [selected, setSelected] = useState<ArtifactItem | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)
  const [pageRef, pageWidth] = useElementWidth<HTMLDivElement>()
  const split = useMediaQuery(WIDE_QUERY) && pageWidth >= SPLIT_MIN_PAGE

  // The list/grid choice is remembered per person; nothing is read until the viewer is known.
  // `?view=` in the address wins over the remembered choice.
  useEffect(() => {
    if (person) setView(url.view ?? readView(person))
  }, [person, url.view])
  // A new workspace starts unfiltered.
  useEffect(() => {
    setFilters({})
    setQInput('')
    setPage(1)
    setSelected(null)
  }, [ws])
  // Another filter or page: the selection belonged to the old list, so the pane closes.
  useEffect(() => {
    setSelected(null)
  }, [filters, page])
  // Typing narrows after a short pause.
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters((f) => (f.q === (qInput.trim() || undefined) ? f : { ...f, q: qInput.trim() || undefined }))
      setPage(1)
    }, 200)
    return () => clearTimeout(t)
  }, [qInput])

  const query = { ...filters, page }
  const list = useQuery({
    queryKey: ['artifacts', ws, query],
    queryFn: () => api.listArtifacts(ws!, query),
    enabled: !!ws,
    placeholderData: keepPreviousData,
  })
  const data = list.data
  const members = useMemo(() => workspace?.members ?? [], [workspace])

  const set = (patch: Partial<ArtifactQuery>) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(1)
  }
  const dirty = Object.values(filters).some((v) => v !== undefined) || qInput !== ''
  const chooseView = (v: View) => {
    setView(v)
    url.setView(v)
    try {
      if (person) localStorage.setItem(viewKey(person), v)
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }
  const listed = view === 'list'
  const openItem = (a: ArtifactItem, el: HTMLElement) => {
    opener.current = el
    if (listed) setSelected(a)
    // Beside the table the pane shows it; narrower (and in the grid) the drawer opens.
    if (!listed || !split) setOpen(a)
  }
  // `?a=` names the shown artifact (the selected row, or the open drawer).
  useShownArtifactInUrl(url, { items: view ? data?.items : undefined, shown: selected ?? open, show: (a) => { if (listed) setSelected(a); if (!listed || !split) setOpen(a) } })
  // Wider than SPLIT_MIN again: the pane takes over from an open drawer.
  useEffect(() => {
    if (split && listed) setOpen(null)
  }, [split, listed])

  // j / k: the next / previous row; the preview follows (the pane, or the drawer while it is open).
  const items = data?.items
  useEffect(() => {
    if (!listed || !items?.length) return
    const onKey = (e: KeyboardEvent) => {
      if ((e.key !== 'j' && e.key !== 'k') || busy(e)) return
      e.preventDefault()
      const at = selected ? items.findIndex((x) => artifactKey(x) === artifactKey(selected)) : -1
      const next = items[at < 0 ? 0 : Math.min(items.length - 1, Math.max(0, at + (e.key === 'j' ? 1 : -1)))]
      setSelected(next)
      setOpen((o) => (o && openMode(next) === 'drawer' ? next : o))
      document.querySelector(`[data-artifact="${CSS.escape(artifactKey(next))}"]`)?.scrollIntoView?.({ block: 'nearest' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [listed, items, selected])

  const kindOptions: { value: string; label: string }[] = (data?.facets.kinds ?? []).map((k) => ({ value: k.kind, label: `${k.kind} (${k.count})` }))
  if (filters.kind && !kindOptions.some((o) => o.value === filters.kind)) kindOptions.push({ value: filters.kind, label: `${filters.kind} (0)` })
  const ticketOptions = (data?.facets.tickets ?? []).map((t) => ({ value: t.key, label: `${t.key} · ${t.title.length > 40 ? t.title.slice(0, 39) + '…' : t.title} (${t.count})` }))
  const byFacets = data?.facets.by ?? []
  const byOptions = [
    { value: 'people', label: 'People' },
    { value: 'agents', label: 'Agents' },
    ...byFacets.map((b) => ({ value: b.id, label: `${b.kind === 'agent' ? agentName(b.id) : b.kind === 'person' ? displayName(members, b.id) : b.kind === 'addon' ? `${b.id} addon` : 'orch'} (${b.count})` })),
  ]

  const from = data ? (data.page - 1) * data.per + 1 : 0
  const to = data ? Math.min(data.total, data.page * data.per) : 0

  const pane = listed && split && selected
  // Closing the pane puts focus back on its row's name.
  const closePane = () => {
    const row = selected && `[data-artifact="${CSS.escape(artifactKey(selected))}"]`
    setSelected(null)
    if (row) requestAnimationFrame(() => document.querySelector<HTMLElement>(`${row} button, ${row} a`)?.focus())
  }
  return (
    <div ref={pageRef} className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Artifacts</h1>
        {data && (
          <span className="text-[13px] text-text-muted" aria-live="polite">
            {data.total === 1 ? '1 artifact' : `${data.total} artifacts`}
          </span>
        )}
        <span className="flex-1" />
        <ToggleGroup type="single" value={view ?? 'list'} onValueChange={(v) => v && chooseView(v as View)} variant="outline" size="sm" aria-label="Layout">
          <ToggleGroupItem value="list" aria-label="List" className="h-8 gap-1.5 px-2.5 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
            <List className="size-3.5" />
            List
          </ToggleGroupItem>
          <ToggleGroupItem value="grid" aria-label="Grid" className="h-8 gap-1.5 px-2.5 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
            <LayoutGrid className="size-3.5" />
            Grid
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
      <p className="-mt-2 text-[13px] text-text-muted">Evidence, logs, screenshots and reports from the tickets you can see in this workspace.</p>

      <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Artifact filters">
        <SearchBox ref={searchRef} value={qInput} onChange={setQInput} placeholder="Search artifacts" label="Search artifacts" />
        <FilterSelect label="Kind" value={filters.kind ?? 'all'} onChange={(v) => set({ kind: v === 'all' ? undefined : v })} options={kindOptions} />
        <FilterSelect label="Ticket" value={filters.ticket ?? 'all'} onChange={(v) => set({ ticket: v === 'all' ? undefined : v })} options={ticketOptions} />
        <FilterSelect label="Added by" value={filters.by ?? 'all'} onChange={(v) => set({ by: v === 'all' ? undefined : v })} options={byOptions} />
        <FilterSelect label="Added" value={filters.since ?? 'all'} onChange={(v) => set({ since: v === 'all' ? undefined : v })} options={SINCE} />
        {dirty && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setFilters({})
              setQInput('')
              setPage(1)
            }}
          >
            <X />
            Clear filters
          </Button>
        )}
      </div>

      {!data || !view ? (
        <div aria-busy="true" className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : data.total === 0 ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[13px] text-text-faint">
          {dirty ? 'No artifacts match these filters.' : 'No artifacts yet. Agents attach evidence with orch artifact add.'}
        </p>
      ) : (
        <div className={cn(pane && 'grid grid-cols-[minmax(0,1fr)_minmax(0,44%)] items-start gap-4')}>
          <div className={list.isPlaceholderData ? 'opacity-60 transition-opacity' : undefined}>
            {view === 'grid' ? (
              <ArtifactGrid items={data.items} members={members} onOpen={openItem} />
            ) : (
              <ArtifactList items={data.items} members={members} onOpen={openItem} selected={selected && artifactKey(selected)} narrow={!!pane} />
            )}
          </div>
          {pane && <ArtifactPane item={selected} members={members} onClose={closePane} />}
        </div>
      )}

      {data && data.pages > 1 && (
        <nav aria-label="Pages" className="flex items-center justify-end gap-2 text-[12px] text-text-muted">
          <span>
            {from}–{to} of {data.total}
          </span>
          <Button variant="outline" size="sm" disabled={data.page <= 1} onClick={() => setPage(data.page - 1)} aria-label="Previous page">
            <ChevronLeft />
          </Button>
          <span>
            Page {data.page} of {data.pages}
          </span>
          <Button variant="outline" size="sm" disabled={data.page >= data.pages} onClick={() => setPage(data.page + 1)} aria-label="Next page">
            <ChevronRight />
          </Button>
        </nav>
      )}

      <ArtifactPreview item={open} members={members} onClose={() => setOpen(null)} opener={opener} />
    </div>
  )
}
