import { useMemo, useState } from 'react'
import { ArrowDown, ArrowUp, Lock } from 'lucide-react'
import { STATUSES, type TicketSummary } from '@/api/types'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { CardFields, type BoardPeople } from './TicketCard'
import { PRIORITY_RANK, PriorityMarker, TypeIcon } from './lib'
import { fmtWhen } from '@/lib/time'
import { statusLabel } from '@/app/pages/ticket/shared'
import { addonActive } from '@/api/addons'
import { useWorkspace } from '@/app/workspace'

type SortKey = 'key' | 'title' | 'status' | 'priority' | 'size' | 'owner' | 'updated_at'
const SIZES = ['xs', 's', 'm', 'l', 'xl']

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: 'key', label: 'Key' },
  { key: 'title', label: 'Title' },
  { key: 'status', label: 'Status' },
  { key: 'priority', label: 'Priority' },
  { key: 'size', label: 'Size' },
  { key: 'owner', label: 'Owner' },
  { key: 'updated_at', label: 'Updated' },
]

export function ListView({ tickets, people, onOpen }: { tickets: TicketSummary[]; people: BoardPeople; onOpen: (key: string) => void }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({ key: 'updated_at', dir: -1 })
  // Size or Points, never both (B m14): with Estimate on, its points show in the Addons column and Size steps back.
  const { workspace } = useWorkspace()
  const showSize = !addonActive(workspace, 'estimate')
  const columns = showSize ? COLUMNS : COLUMNS.filter((c) => c.key !== 'size')

  const rows = useMemo(() => {
    const val = (t: TicketSummary): string | number => {
      switch (sort.key) {
        case 'status':
          return STATUSES.indexOf(t.status)
        case 'priority':
          return PRIORITY_RANK[t.priority]
        case 'size':
          return t.size ? SIZES.indexOf(t.size) : -1
        case 'owner':
          return people.name(t.owner)
        default:
          return t[sort.key]
      }
    }
    return [...tickets].sort((a, b) => {
      const x = val(a)
      const y = val(b)
      return (x < y ? -1 : x > y ? 1 : 0) * sort.dir
    })
  }, [tickets, sort, people])

  return (
    <div className="min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-surface">
      <Table>
        <TableHeader className="sticky top-0 bg-surface-2">
          <TableRow>
            {columns.map((c) => (
              <TableHead key={c.key} aria-sort={sort.key === c.key ? (sort.dir === 1 ? 'ascending' : 'descending') : 'none'} className="h-8 p-0">
                <button
                  type="button"
                  onClick={() => setSort((s) => (s.key === c.key ? { key: c.key, dir: (s.dir * -1) as 1 | -1 } : { key: c.key, dir: 1 }))}
                  className="flex h-8 w-full items-center gap-1 px-2 text-[12px] font-medium text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
                >
                  {c.label}
                  {sort.key === c.key && (sort.dir === 1 ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                </button>
              </TableHead>
            ))}
            <TableHead className="h-8 text-[12px] text-text-muted">Addons</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((t) => (
            <TableRow
              key={t.key}
              tabIndex={0}
              role="link"
              aria-label={`${t.key} ${t.title}`}
              onClick={() => onOpen(t.key)}
              onKeyDown={(e) => e.key === 'Enter' && onOpen(t.key)}
              className="cursor-pointer outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand"
            >
              <TableCell className="py-1.5 font-mono text-[12px] text-text-muted">
                <span className="inline-flex items-center gap-1.5">
                  <TypeIcon type={t.type} />
                  {t.key}
                  {t.restricted && <Lock role="img" aria-label="Restricted" className="size-3 text-text-faint" />}
                </span>
              </TableCell>
              <TableCell className="max-w-[420px] truncate py-1.5 text-[13px] text-text">{t.title}</TableCell>
              <TableCell className="py-1.5 text-[12px] text-text-muted">{statusLabel(t.status, t.landing)}</TableCell>
              <TableCell className="py-1.5">
                <span className="inline-flex items-center gap-1 text-[12px] text-text-muted">
                  <PriorityMarker priority={t.priority} />
                  {t.priority}
                </span>
              </TableCell>
              {showSize && <TableCell className={cn('py-1.5 font-mono text-[11px] uppercase text-text-muted')}>{t.size ?? '–'}</TableCell>}
              <TableCell className="py-1.5 text-[12px] text-text-muted">{t.owner ? people.name(t.owner) : '–'}</TableCell>
              <TableCell className="py-1.5 font-mono text-[11px] text-text-faint">{fmtWhen(t.updated_at)}</TableCell>
              <TableCell className="py-1.5">
                <span className="flex gap-1">
                  <CardFields ticket={t} />
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {rows.length === 0 && <p className="p-6 text-center text-[13px] text-text-faint">No tickets match the filters.</p>}
    </div>
  )
}
