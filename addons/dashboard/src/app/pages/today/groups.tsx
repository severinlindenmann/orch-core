// One group of Today's queue: a header button with the count and the oldest age, five rows, "Show N more".
// Whether a group is open and whether it shows all rows is kept for the browser session.
import type { ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { itemsIn, type Group, type Row } from './queue'
import { ago, useSessionState } from './shared'

/** Rows a group shows before "Show N more". */
export const ROWS_SHOWN = 5

export function QueueGroup({ group, now, scope, note, renderRow, pinned }: {
  group: Group
  now: string
  /** Keys the remembered open / show-all state (workspace and person). */
  scope: string
  /** A sentence that belongs in the header once, not on every row. */
  note?: string
  renderRow: (row: Row) => ReactNode
  /** A row that stays visible even past the first five (the open one). */
  pinned?: string | null
}) {
  const [open, setOpen] = useSessionState(`orch.today.open.${scope}.${group.id}`, true)
  const [all, setAll] = useSessionState(`orch.today.all.${scope}.${group.id}`, false)
  const headerId = `today-group-${group.id}`
  const visible = all ? group.rows : group.rows.filter((r, i) => i < ROWS_SHOWN || r.id === pinned)
  const hidden = itemsIn(group.rows) - itemsIn(visible)
  return (
    <section role="region" aria-labelledby={headerId} className="overflow-hidden rounded-lg border border-border bg-surface">
      <h2 className="text-[13px]">
        <button
          id={headerId}
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-1 px-3 py-2.5 text-left outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <ChevronDown className={cn('mr-1 size-4 shrink-0 text-text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
          <span className="font-semibold text-text">{group.label}</span>{' '}
          <span className="min-w-0 truncate tabular-nums text-text-muted">
            {`· ${group.count}`}
            {group.oldest ? ` · oldest ${ago(group.oldest, now)}` : ''}
            {note ? ` · ${note}` : ''}
          </span>
        </button>
      </h2>
      {open && (
        <>
          <ul className="border-t border-border">{visible.map((r) => renderRow(r))}</ul>
          {(hidden > 0 || all) && group.rows.length > ROWS_SHOWN && (
            <button
              type="button"
              onClick={() => setAll(!all)}
              className="w-full border-t border-border px-3 py-2 text-left text-xs text-text-muted outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
            >
              {all ? 'Show fewer' : `Show ${hidden} more`}
            </button>
          )}
        </>
      )}
    </section>
  )
}
