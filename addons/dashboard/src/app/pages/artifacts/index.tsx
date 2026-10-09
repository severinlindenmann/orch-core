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
import { ArtifactPreview } from './Preview'
import { ArtifactGrid, ArtifactList } from './views'

type View = 'list' | 'grid'
const viewKey = (person: string) => `orch.artifacts.view.${person}`
function readView(person: string): View {
  try {
    return localStorage.getItem(viewKey(person)) === 'grid' ? 'grid' : 'list'
  } catch {
    return 'list'
  }
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
  const [open, setOpen] = useState<ArtifactItem | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  // The list/grid choice is remembered per person; nothing is read until the viewer is known.
  useEffect(() => {
    if (person) setView(readView(person))
  }, [person])
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
  const members = useMemo(() => workspace?.members ?? [], [workspace])

  const set = (patch: Partial<ArtifactQuery>) => {
    setFilters((f) => ({ ...f, ...patch }))
    setPage(1)
  }
  const dirty = Object.values(filters).some((v) => v !== undefined) || qInput !== ''
  const chooseView = (v: View) => {
    setView(v)
    try {
      if (person) localStorage.setItem(viewKey(person), v)
    } catch {
      /* storage unavailable: the choice lasts for this page only */
    }
  }
  const openItem = (a: ArtifactItem, el: HTMLElement) => {
    opener.current = el
    setOpen(a)
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
    <div className="space-y-4">
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
        <div className={list.isPlaceholderData ? 'opacity-60 transition-opacity' : undefined}>
          {view === 'grid' ? <ArtifactGrid items={data.items} members={members} onOpen={openItem} /> : <ArtifactList items={data.items} members={members} onOpen={openItem} />}
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
