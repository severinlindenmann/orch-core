import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useRouter, useSearch } from '@tanstack/react-router'
import { ChevronDown, Tag, Terminal, X } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import { ApiError, STATUSES, type Status, type TicketSummary } from '@/api/types'
import { useAddons } from '@/addon-ui'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useWorkspace } from '@/app/workspace'
import { useRole } from '@/app/useRole'
import { usePageHeader } from '@/app/shell/ShellUi'
import type { BoardPeople } from '../board/TicketCard'
import { STATUS_LABEL } from '../board/lib'
import { groupByEpic, hasEpics, isCollapsed, NO_EPIC } from '../board/grouping'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useTicketsGroup } from './group'
import { Filters } from './Filters'
import { SavedViews } from './SavedViews'
import { useElementWidth } from '@/lib/useElementWidth'
import { TicketRowsSkeleton } from '../skeletons'
import { TICKETS_FOLD_BELOW, TicketsTable, type AddonColumn } from './TicketsTable'
import { hasFilters, type SortKey, type TicketsSearch, ticketsServerParams } from './search'
import { toastApiError } from '@/app/toast'
import { LoadFailed } from '@/components/LoadFailed'
import { plural } from '@/lib/time'
import { queries } from '@/api/queries'

const CLI_HINT = 'orch list --status open'

/** Keys typed into a text field, or while a menu/dialog is open, must not trigger list shortcuts. */
function typingTarget(el: EventTarget | null): boolean {
  const t = el as HTMLElement | null
  return !!t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) || !!t.closest?.('[role="dialog"],[role="menu"],[role="listbox"]'))
}

function BulkBar({ keys, onDone }: { keys: string[]; onDone: () => void }) {
  const qc = useQueryClient()
  const [label, setLabel] = useState('')
  const [labelOpen, setLabelOpen] = useState(false)
  const run = useMutation({
    mutationFn: async (action: Parameters<typeof api.postAction>[1]) => {
      const failures: ApiError[] = []
      for (const key of keys) {
        try {
          await api.postAction(key, action)
        } catch (err) {
          failures.push(err instanceof ApiError ? new ApiError(err.status, { code: err.code, message: `${key}: ${err.message}`, hint: err.hint, retryable: false }) : new ApiError(0, { code: 'request_failed', message: `${key}: request failed`, retryable: false }))
        }
      }
      if (failures.length) {
        const [first] = failures
        throw new ApiError(first.status, { code: first.code, message: first.message + (failures.length > 1 ? ` (+${failures.length - 1} more)` : ''), hint: first.hint, retryable: false })
      }
    },
    onError: (err) => toastApiError(err),
    onSettled: (_d, err) => {
      for (const k of ['tickets', 'ticket', 'workspaces', 'today', 'board']) void qc.invalidateQueries({ queryKey: [k] })
      if (!err) onDone()
    },
  })
  return (
    <div role="region" aria-label="Bulk actions" className="flex items-center gap-2 rounded-lg border border-brand bg-brand-soft px-3 py-1.5">
      <span className="text-[13px] font-medium text-text">{keys.length} selected</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm" className="h-7 gap-1 text-[12px]" disabled={run.isPending}>
            Set status <ChevronDown className="size-3" aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          {STATUSES.filter((s) => s !== 'done').map((s) => (
            <DropdownMenuItem key={s} onSelect={() => run.mutate({ action: 'set_status', status: s })}>
              {STATUS_LABEL[s]}
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      <Popover open={labelOpen} onOpenChange={setLabelOpen}>
        <PopoverTrigger asChild>
          <Button variant="outline" size="sm" className="h-7 gap-1 text-[12px]" disabled={run.isPending}>
            <Tag className="size-3" aria-hidden /> Add label
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="w-56 p-2">
          <form
            className="flex gap-1.5"
            onSubmit={(e) => {
              e.preventDefault()
              if (!label.trim()) return
              run.mutate({ action: 'add_label', label })
              setLabel('')
              setLabelOpen(false)
            }}
          >
            <Input value={label} onChange={(e) => setLabel(e.target.value)} aria-label="Label" placeholder="label" className="h-8 text-[12px]" />
            <Button type="submit" size="sm" className="h-8 text-[12px]">
              Apply
            </Button>
          </form>
        </PopoverContent>
      </Popover>
      <span className="flex-1" />
      <Button variant="ghost" size="sm" className="h-7 gap-1 text-[12px] text-text-muted" onClick={onDone}>
        <X className="size-3" aria-hidden /> Clear selection
      </Button>
    </div>
  )
}

export function TicketsPage() {
  usePageHeader('Tickets')
  const search: TicketsSearch = useSearch({ from: '/tickets' })
  const navigate = useNavigate({ from: '/tickets' })
  const { workspace } = useWorkspace()
  const wsId = workspace?.id
  const { data: addons = [] } = useAddons()

  const meQ = useQuery(queries.me())
  const me = meQ.data
  const role = useRole()
  const canBulk = can(role, 'ticket.move')

  // Status is filtered here (not on the server) so the status chips can show counts for the other filters.
  const serverParams = useMemo(
    () => ticketsServerParams(search),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [search.repo, search.q, search.type, search.priority, search.person, search.needs, search.label, search.sort],
  )
  const { data: all, isPending } = useQuery({
    ...queries.tickets(wsId!, serverParams),
    enabled: !!wsId,
    placeholderData: (prev) => prev,
  })
  // Options for the selects come from the unfiltered list.
  const { data: everything = [] } = useQuery({ ...queries.ticketsAll(wsId!), enabled: !!wsId })

  const rows = useMemo(() => (all ?? []).filter((t) => !search.status?.length || search.status.includes(t.status)), [all, search.status])
  const [grouping, setGrouping] = useTicketsGroup(me?.person)
  const dirty = hasFilters(search)
  const grouped = grouping.group === 'epic' && hasEpics(everything)
  const filtering = dirty || !!search.status?.length
  const groups = useMemo(() => (grouped ? groupByEpic(everything, rows, filtering) : null), [grouped, everything, rows, filtering])
  // What j/k walks and what bulk actions act on: the rows on screen ("No epic" first, as in the table).
  const navRows = useMemo(
    () =>
      groups
        ? [...(grouping.sections[NO_EPIC] ? [] : groups.none), ...groups.lanes.filter((l) => !isCollapsed(l, grouping.sections, filtering)).flatMap((l) => l.children)]
        : rows,
    [groups, grouping.sections, rows, filtering],
  )
  const counts = useMemo(() => {
    const c = Object.fromEntries(STATUSES.map((s) => [s, 0])) as Record<Status, number>
    for (const t of all ?? []) c[t.status]++
    return c
  }, [all])
  const options = useMemo(
    () => ({
      types: [...new Set(everything.map((t) => t.type))].sort(),
      labels: [...new Set(everything.flatMap((t) => t.labels))].sort(),
      people: (workspace?.members ?? []).map((m) => ({ value: m.person, label: m.name })),
    }),
    [everything, workspace],
  )
  const people = useMemo<BoardPeople>(() => {
    const byId = new Map(workspace?.members.map((m) => [m.person, m.name]))
    return { name: (id) => (id ? (byId.get(id) ?? id) : 'nobody'), epicTitle: () => undefined }
  }, [workspace])
  const addonColumns = useMemo<AddonColumn[]>(
    () =>
      addons
        .filter((a) => addonActive(workspace, a.name))
        .flatMap((a) => a.contributions.filter((c) => c.slot === 'board.card_field').map((c) => ({ addon: a.name, title: c.title }))),
    [addons, workspace],
  )

  // Filters live in the URL: Back restores them, and a saved view is just these params.
  const setSearch = useCallback(
    (patch: Partial<TicketsSearch>, replace = false) =>
      void navigate({
        search: (prev: TicketsSearch) => Object.fromEntries(Object.entries({ ...prev, ...patch }).filter(([, v]) => v !== undefined && v !== '')) as TicketsSearch,
        replace,
      }),
    [navigate],
  )

  // Search box: local text, pushed to the URL after 200 ms.
  const [qInput, setQInput] = useState(search.q ?? '')
  const sentQ = useRef(search.q ?? '')
  useEffect(() => {
    if ((search.q ?? '') !== sentQ.current) {
      sentQ.current = search.q ?? ''
      setQInput(search.q ?? '')
    }
  }, [search.q])
  useEffect(() => {
    if (qInput === sentQ.current) return
    const id = setTimeout(() => {
      sentQ.current = qInput
      setSearch({ q: qInput.trim() || undefined }, true)
    }, 200)
    return () => clearTimeout(id)
  }, [qInput, setSearch])

  const clear = () => {
    sentQ.current = ''
    setQInput('')
    void navigate({ search: {} })
  }

  // Row focus and selection
  const [focusKey, setFocusKey] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const toggle = useCallback((key: string) => setSelected((s) => (s.has(key) ? new Set([...s].filter((k) => k !== key)) : new Set(s).add(key))), [])
  const visibleKeys = useMemo(() => new Set(navRows.map((r) => r.key)), [navRows])
  const picked = [...selected].filter((k) => visibleKeys.has(k))

  const searchRef = useRef<HTMLInputElement>(null)
  const state = useRef({ rows: navRows, focusKey, canBulk })
  state.current = { rows: navRows, focusKey, canBulk }
  const router = useRouter()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return
      // Another page is loading: this one may be hidden under its skeleton, so its keys are off (G4 review M4).
      if (router.state.status === 'pending' && router.state.location.pathname !== '/tickets') return
      const target = e.target as HTMLElement | null
      if (typingTarget(target)) return
      const { rows: list, focusKey: cur, canBulk: bulk } = state.current
      const idx = list.findIndex((r) => r.key === cur)
      switch (e.key) {
        case '/':
          e.preventDefault()
          searchRef.current?.focus()
          break
        case 'j':
        case 'k': {
          if (!list.length) return
          const next = e.key === 'j' ? Math.min(list.length - 1, idx + 1) : Math.max(0, idx < 0 ? 0 : idx - 1)
          setFocusKey(list[next].key)
          break
        }
        case 'x':
          if (bulk && cur) toggle(cur)
          break
        case 'Enter':
          // Only from the page itself or a row, so Enter on a button or link keeps its own meaning.
          if (cur && (target === document.body || target?.tagName === 'TR')) void navigate({ to: '/ticket/$key', params: { key: cur } })
          break
        case 'Escape':
          setSelected(new Set())
          break
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate, toggle])

  // A narrow page area (the terminal docked on the right): filters in one popover, saved views in a select (N11).
  const [frame, pageWidth] = useElementWidth<HTMLDivElement>()
  const compact = pageWidth > 0 && pageWidth < TICKETS_FOLD_BELOW

  const sort: SortKey = search.sort ?? 'updated'
  const shown: TicketSummary[] = rows
  // Grouped, epics are headers and not tickets: "150 tickets · 6 epics".
  const cardRows = groups ? rows.filter((t) => t.type !== 'epic').length : rows.length
  const allCards = groups ? everything.filter((t) => t.type !== 'epic').length : everything.length
  const countLabel = cardRows === (allCards || cardRows) ? `${plural(cardRows, 'ticket')}${groups ? ` · ${plural(groups.lanes.length, 'epic')}` : ''}` : `${cardRows} of ${allCards}`
  return (
    <div ref={frame} className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex items-baseline gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Tickets</h1>
        <span className="font-mono text-[11px] text-text-faint" aria-live="polite">
          {countLabel}
        </span>
        <span className="flex-1" />
        {hasEpics(everything) && (
          <div className="flex items-center gap-1.5">
            <span className="text-[11px] text-text-faint">Group</span>
            <ToggleGroup type="single" variant="outline" size="sm" value={grouping.group} onValueChange={(v) => v && setGrouping({ group: v as 'epic' | 'none' })} aria-label="Group by">
              <ToggleGroupItem value="epic" className="h-7 px-2.5 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
                Epic
              </ToggleGroupItem>
              <ToggleGroupItem value="none" className="h-7 px-2.5 text-[12px] data-[state=on]:bg-brand-soft data-[state=on]:text-brand">
                None
              </ToggleGroupItem>
            </ToggleGroup>
          </div>
        )}
      </div>
      {wsId && (
        <SavedViews
          wsId={wsId}
          search={search}
          me={me?.person}
          canShare={can(role, 'view.share')}
          onApply={(params) => void navigate({ search: params })}
          onClear={clear}
          compact={compact}
        />
      )}
      <Filters
        search={search}
        onSearch={(p) => setSearch(p)}
        options={options}
        counts={counts}
        qInput={qInput}
        onQInput={setQInput}
        searchRef={searchRef}
        dirty={dirty || qInput !== ''}
        onClear={clear}
        compact={compact}
      />
      {canBulk && picked.length > 0 && <BulkBar keys={picked} onDone={() => setSelected(new Set())} />}
      {meQ.isError ? (
        <LoadFailed what="tickets" onRetry={() => void meQ.refetch()} />
      ) : isPending || !me ? (
        <TicketRowsSkeleton />
      ) : shown.length === 0 ? (
        <div className="rounded-lg border border-border bg-surface p-8 text-center" role="status">
          <p className="text-[13px] text-text">No tickets match.</p>
          <Button variant="link" size="sm" className="text-[13px]" onClick={clear}>
            Clear filters
          </Button>
          <p className="mt-2 inline-flex items-center gap-1.5 text-[12px] text-text-faint">
            <Terminal className="size-3.5" aria-hidden /> From the terminal: <code className="font-mono text-text-muted">{CLI_HINT}</code>
          </p>
        </div>
      ) : (
        <>
          <TicketsTable
            tickets={shown}
            groups={groups}
            filtering={filtering}
            sections={grouping.sections}
            onSection={(key, collapse) => setGrouping({ sections: { ...grouping.sections, [key]: collapse } })}
            people={people}
            me={me?.person}
            selected={selected}
            canSelect={canBulk}
            focusKey={focusKey}
            sort={sort}
            onSort={(k) => setSearch({ sort: k === 'updated' ? undefined : k })}
            onToggle={toggle}
            onFocus={setFocusKey}
            addonColumns={addonColumns}
          />
          <p className="text-[11px] text-text-faint">
            <kbd className="font-mono">/</kbd> search · <kbd className="font-mono">j</kbd>/<kbd className="font-mono">k</kbd> move · <kbd className="font-mono">Enter</kbd> open
            {canBulk && (
              <>
                {' '}
                · <kbd className="font-mono">x</kbd> select
              </>
            )}{' '}
            · CLI: <code className="font-mono">{CLI_HINT}</code>
          </p>
        </>
      )}
    </div>
  )
}
