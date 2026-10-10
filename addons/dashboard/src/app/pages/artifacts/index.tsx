import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, LayoutGrid, List, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { ArtifactQuery } from '@/api/types'
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
import { useAddonPages } from '../ticket/Artifacts'
import { ArtifactPane, ArtifactPreview, EmptyPane, openMode } from './Preview'
import { artifactKey, previewTarget, useArtifactSelection, type View } from './selection'
import { ArtifactGrid, ArtifactList } from './views'

/**
 * The preview sits beside the results on a window of at least 1280 px (the Today rule) whose page area still has
 * SPLIT_MIN_PAGE px (not beside a wide terminal dock); otherwise it opens as a drawer (DECISIONS-LOG F2, G3). The
 * same rule for the list and the grid.
 */
export const SPLIT_MIN_PAGE = 960
/** Below this page width the list drops "Added by" and "Size" (the viewer names both). */
const LIST_FULL_MIN = 900

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
  const searchRef = useRef<HTMLInputElement>(null)
  const [pageRef, pageWidth] = useElementWidth<HTMLDivElement>()
  const split = useMediaQuery(WIDE_QUERY) && pageWidth >= SPLIT_MIN_PAGE

  // A new workspace starts unfiltered.
  useEffect(() => {
    setFilters({})
    setQInput('')
    setPage(1)
  }, [ws])
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
  const items = data?.items
  // Results from the previous filters stay visible while the new ones load, but cannot be acted on.
  const updating = list.isPlaceholderData
  const members = useMemo(() => workspace?.members ?? [], [workspace])
  const addonPage = useAddonPages()
  const { view, chooseView, current, currentKey, preview, close, lastKey } = useArtifactSelection({
    person,
    items,
    settled: !!data && !updating,
    context: JSON.stringify([ws, filters, page]),
  })

  const set = (patch: Partial<ArtifactQuery>) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(1)
  }
  const dirty = Object.values(filters).some((v) => v !== undefined) || qInput !== ''

  // Close (pane or drawer) puts focus back on the item's Preview button, or the results heading if it is gone.
  const restore = useMemo(() => ({ get current() { return previewTarget(lastKey.current) } }), [lastKey])
  const closePane = () => {
    close()
    requestAnimationFrame(() => restore.current?.focus())
  }

  // j / k inside the results: the next / previous artifact with a preview. Focus moves to its Preview button and the
  // pane follows (a drawer would take the focus away, so beside a narrow page j/k only move the focus).
  const [said, setSaid] = useState('')
  const onResultsKey = (e: React.KeyboardEvent) => {
    if ((e.key !== 'j' && e.key !== 'k') || e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || !items) return
    const t = e.target as HTMLElement
    if (t.isContentEditable || t.closest('input, textarea, select, [role="menu"], [role="listbox"]') || document.querySelector('[role="dialog"], [role="alertdialog"]')) return
    const from = t.closest('[data-artifact]')?.getAttribute('data-artifact') ?? currentKey
    const at = from ? items.findIndex((a) => artifactKey(a) === from) : -1
    const step = e.key === 'j' ? 1 : -1
    let i = at < 0 ? (step > 0 ? 0 : items.length - 1) : at + step
    while (i >= 0 && i < items.length && openMode(items[i]) !== 'preview') i += step
    e.preventDefault()
    const next = items[i]
    if (!next) return
    previewTarget(artifactKey(next))?.focus()
    if (split) {
      preview(next)
      setSaid(`Previewing ${next.name}`)
    }
  }
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

  return (
    <div ref={pageRef} className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Artifacts</h1>
        {data && (
          <span className="text-[13px] text-text-muted" aria-live="polite">
            {updating ? 'Updating…' : data.total === 1 ? '1 artifact' : `${data.total} artifacts`}
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

      <h2 id="artifact-results" tabIndex={-1} className="sr-only">
        Results
      </h2>
      <p className="sr-only" aria-live="polite">
        {said}
      </p>
      {!data || !view ? (
        <div aria-busy="true" className="space-y-2">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-10 w-full" />
        </div>
      ) : data.total === 0 && !updating ? (
        <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-[13px] text-text-faint">
          {dirty ? 'No artifacts match these filters.' : 'No artifacts yet. Agents attach evidence with orch artifact add.'}
        </p>
      ) : (
        <div className={cn(split && 'grid grid-cols-[minmax(0,1fr)_minmax(0,44%)] items-start gap-4')}>
          {/* The results: a container (the grid's columns follow its width) and the scope of j/k. */}
          <div className={cn('@container/results min-w-0', updating && 'opacity-60 transition-opacity')} inert={updating} aria-busy={updating || undefined} onKeyDown={onResultsKey}>
            {view === 'grid' ? (
              <ArtifactGrid items={data.items} members={members} current={currentKey} onPreview={preview} addonPage={addonPage} />
            ) : (
              <ArtifactList items={data.items} members={members} current={currentKey} onPreview={preview} addonPage={addonPage} compact={split || pageWidth < LIST_FULL_MIN} />
            )}
          </div>
          {split && (current ? <ArtifactPane item={current} members={members} onClose={closePane} /> : <EmptyPane />)}
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

      <ArtifactPreview item={split ? null : current} members={members} onClose={close} opener={restore} />
    </div>
  )
}
