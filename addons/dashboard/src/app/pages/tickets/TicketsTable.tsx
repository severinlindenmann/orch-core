import { useEffect, useRef } from 'react'
import { Link, useNavigate } from '@tanstack/react-router'
import { ArrowDown, ArrowUp, Lock } from 'lucide-react'
import type { BodySections, TicketSummary } from '@/api/types'
import { AddonBadge } from '@/addon-ui'
import { Checkbox } from '@/components/ui/checkbox'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { CardFields, People, ProgressBar, type BoardPeople } from '../board/TicketCard'
import { PriorityMarker, STATUS_LABEL, TypeIcon } from '../board/lib'
import { ago } from '../ticket/shared'
import type { SortKey } from './search'

const SECTION_LABEL: Record<keyof BodySections, string> = {
  summary: 'Summary',
  context: 'Context',
  requirements: 'Requirements',
  out_of_scope: 'Out of scope',
  plan: 'Plan',
  decisions: 'Decisions',
  verification: 'Verification',
  current_state: 'Current state',
}

export interface AddonColumn {
  addon: string
  title: string
}

function turnLabel(t: TicketSummary, me: string | undefined, people: BoardPeople): string {
  if (t.status === 'done' || t.turn.who === 'nobody') return 'nobody'
  if (t.turn.who === me) return 'you'
  if (t.turn.who.startsWith('agent:')) return t.turn.who.slice(6)
  return people.name(t.turn.who)
}

function SortHeader({ label, sr, id, sort, onSort, className }: { label: string; sr?: string; id?: SortKey; sort: SortKey; onSort: (k: SortKey) => void; className?: string }) {
  const active = id !== undefined && sort === id
  const text = (
    <>
      {label}
      {sr && <span className="sr-only">{sr}</span>}
    </>
  )
  return (
    <TableHead aria-sort={active ? (id === 'updated' ? 'descending' : 'ascending') : undefined} className={cn('h-8 px-1.5 text-[12px] font-medium text-text-muted', className)}>
      {id ? (
        <button type="button" onClick={() => onSort(id)} className="-mx-1 inline-flex items-center gap-1 rounded px-1 outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand">
          {text}
          {active && (id === 'updated' ? <ArrowDown className="size-3" aria-hidden /> : <ArrowUp className="size-3" aria-hidden />)}
        </button>
      ) : (
        text
      )}
    </TableHead>
  )
}

export function TicketsTable({
  tickets,
  people,
  me,
  selected,
  canSelect,
  focusKey,
  sort,
  onSort,
  onToggle,
  onFocus,
  addonColumns,
}: {
  tickets: TicketSummary[]
  people: BoardPeople
  me: string | undefined
  selected: Set<string>
  canSelect: boolean
  focusKey: string | null
  sort: SortKey
  onSort: (k: SortKey) => void
  onToggle: (key: string) => void
  onFocus: (key: string) => void
  addonColumns: AddonColumn[]
}) {
  const navigate = useNavigate()
  const body = useRef<HTMLTableSectionElement>(null)
  useEffect(() => {
    if (!focusKey) return
    const row = [...(body.current?.rows ?? [])].find((r) => r.dataset.key === focusKey)
    row?.focus()
    row?.scrollIntoView?.({ block: 'nearest' })
  }, [focusKey])

  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-surface">
      <Table aria-label="Tickets" className="min-w-[800px] table-fixed">
        <TableHeader className="sticky top-0 z-10 bg-surface-2">
          <TableRow>
            {canSelect && <TableHead className="h-8 w-8 px-2"><span className="sr-only">Select</span></TableHead>}
            <SortHeader label="Key" id="key" sort={sort} onSort={onSort} className="w-[92px]" />
            <SortHeader label="Title" sort={sort} onSort={onSort} />
            <SortHeader label="Type" sort={sort} onSort={onSort} className="w-[44px]" />
            <SortHeader label="Pri" sr="ority" id="priority" sort={sort} onSort={onSort} className="w-[40px]" />
            <SortHeader label="Status" id="status" sort={sort} onSort={onSort} className="w-[84px]" />
            <SortHeader label="People" sort={sort} onSort={onSort} className="w-[52px]" />
            <SortHeader label="Turn" sort={sort} onSort={onSort} className="w-[60px]" />
            <SortHeader label="Progress" sort={sort} onSort={onSort} className="w-[72px]" />
            <SortHeader label="Updated" id="updated" sort={sort} onSort={onSort} className="w-[80px]" />
            {addonColumns.length > 0 && (
              <TableHead className="h-8 w-[84px] px-1.5 text-[12px] font-medium text-text-muted">
                <span className="inline-flex items-center gap-1">
                  {addonColumns.map((c) => (
                    <AddonBadge key={c.addon} name={c.addon} />
                  ))}
                  <span className="truncate" title={addonColumns.map((c) => c.title).join(', ')}>
                    {addonColumns[0].title}
                  </span>
                </span>
              </TableHead>
            )}
          </TableRow>
        </TableHeader>
        <TableBody ref={body}>
          {tickets.map((t) => {
            const isSel = selected.has(t.key)
            const turn = turnLabel(t, me, people)
            return (
              <TableRow
                key={t.key}
                data-key={t.key}
                tabIndex={-1}
                aria-selected={canSelect ? isSel : undefined}
                data-state={isSel ? 'selected' : undefined}
                onFocus={(e) => e.target === e.currentTarget && onFocus(t.key)}
                onClick={() => void navigate({ to: '/ticket/$key', params: { key: t.key } })}
                className={cn('cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand', focusKey === t.key && 'bg-surface-2')}
              >
                {canSelect && (
                  <TableCell className="px-1.5 py-1.5" onClick={(e) => e.stopPropagation()}>
                    <Checkbox checked={isSel} onCheckedChange={() => onToggle(t.key)} aria-label={`Select ${t.key}`} />
                  </TableCell>
                )}
                <TableCell className="px-1.5 py-1.5 font-mono text-[12px] text-text-muted">
                  <Link to="/ticket/$key" params={{ key: t.key }} onClick={(e) => e.stopPropagation()} className="inline-flex items-center gap-1 rounded outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand">
                    {t.key}
                    {t.restricted && <Lock role="img" aria-label="Restricted" className="size-3 text-text-faint" />}
                  </Link>
                </TableCell>
                <TableCell className="px-1.5 py-1.5">
                  <div className="truncate text-[13px] text-text" title={t.title}>
                    {t.title}
                  </div>
                  {(t.labels.length > 0 || t.match) && (
                    <div className="mt-0.5 flex min-w-0 items-center gap-1 overflow-hidden">
                      {t.labels.slice(0, 3).map((l) => (
                        <span key={l} className="shrink-0 rounded-full border border-border px-1.5 text-[10px] leading-4 text-text-muted">
                          {l}
                        </span>
                      ))}
                      {t.match && (
                        <span className="min-w-0 truncate text-[11px] text-text-faint" title={t.match.snippet}>
                          <span className="sr-only">{SECTION_LABEL[t.match.section]}: </span>
                          {t.match.snippet}
                        </span>
                      )}
                    </div>
                  )}
                </TableCell>
                <TableCell className="px-1.5 py-1.5">
                  <TypeIcon type={t.type} />
                </TableCell>
                <TableCell className="px-1.5 py-1.5">
                  <PriorityMarker priority={t.priority} />
                </TableCell>
                <TableCell className="truncate px-1.5 py-1.5 text-[12px] text-text-muted">{STATUS_LABEL[t.status]}</TableCell>
                <TableCell className="px-1.5 py-1.5">
                  <People ticket={t} people={people} />
                </TableCell>
                <TableCell className={cn('truncate px-1.5 py-1.5 text-[12px]', turn === 'you' ? 'font-medium text-brand' : 'text-text-muted')} title={t.turn.why}>
                  {turn}
                </TableCell>
                <TableCell className="px-1.5 py-1.5">
                  <ProgressBar ticket={t} />
                </TableCell>
                <TableCell className="truncate px-1.5 py-1.5 font-mono text-[11px] text-text-faint" title={t.updated_at}>
                  {ago(t.updated_at)}
                </TableCell>
                {addonColumns.length > 0 && (
                  <TableCell className="px-1.5 py-1.5">
                    <span className="flex gap-1">
                      <CardFields ticket={t} />
                    </span>
                  </TableCell>
                )}
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}
