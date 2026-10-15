// One group of Today's queue: a header button with the count and the oldest age, five rows, "Show N more".
// Whether a group is open and whether it shows all rows is kept for the browser session.
import { useEffect, useLayoutEffect, useRef, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'
import { collapseOut, fadeIn, prefersReducedMotion } from '@/lib/motion'
import { itemsIn, type Group, type Row } from './queue'
import { ago, useSessionState } from './shared'

/** Rows a group shows before "Show N more". */
export const ROWS_SHOWN = 5

export function QueueGroup({ group, now, scope, note, renderRow, pinned, reveal = 0 }: {
  /** Changes when new items were taken into this group: the group opens (once per change). */
  reveal?: number
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
  useEffect(() => {
    if (reveal > 0) setOpen(true)
  }, [reveal])
  const headerId = `today-group-${group.id}`
  const visible = all ? group.rows : group.rows.filter((r, i) => i < ROWS_SHOWN || r.id === pinned)
  const hidden = itemsIn(group.rows) - itemsIn(visible)
  const listRef = useRowMotion(open, scope, group.rows, visible)
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
          <ul ref={listRef} className="border-t border-border">{visible.map((r) => renderRow(r))}</ul>
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

/**
 * Motion for the rows of one group, only where something changed: a row that is resolved collapses out where it stood
 * (height and fade, 160 ms), a row that arrives (after "Show new") fades in once. Revealing hidden rows with "Show
 * more", a plain refresh and everything under reduced motion stay still. The row that left is the element React
 * just detached; it is put back at its old spot as an inert ghost for the length of the animation. This assumes one
 * element per row, in order (renderRow returns a single <li>); a row that renders two elements breaks the pairing.
 * A new workspace or person (`scope`) starts over, and a row whose id comes back drops its ghost at once, so an id
 * or test id never exists twice.
 */
function useRowMotion(open: boolean, scope: string, rows: Row[], visible: Row[]) {
  const listRef = useRef<HTMLUListElement>(null)
  const last = useRef<{ scope: string; all: string[]; ids: string[]; els: HTMLElement[] } | null>(null)
  useLayoutEffect(() => {
    const ul = listRef.current
    if (!open || !ul) {
      last.current = null
      return
    }
    const live = [...ul.children].filter((c): c is HTMLElement => c instanceof HTMLElement && !c.hasAttribute('data-ghost'))
    const ids = visible.map((r) => r.id)
    const all = rows.map((r) => r.id)
    const before = last.current?.scope === scope ? last.current : null
    last.current = { scope, all, ids, els: live }
    const liveTestIds = new Set(live.map((e) => e.dataset.testid).filter(Boolean))
    for (const g of ul.querySelectorAll<HTMLElement>('[data-ghost]')) if (liveTestIds.has(g.dataset.testid)) g.remove()
    if (!before || prefersReducedMotion()) return
    // Arrived: on screen now, in no earlier list of this group.
    ids.forEach((id, i) => {
      if (!before.all.includes(id)) fadeIn(live[i])
    })
    // Resolved: was on screen, is no longer in the group at all (hiding by "Show fewer" is not resolving).
    before.ids.forEach((id, i) => {
      const el = before.els[i]
      if (all.includes(id) || !el || el.isConnected) return
      const survivors = before.ids.slice(0, i).filter((x) => ids.includes(x)).length
      el.setAttribute('data-ghost', '')
      el.setAttribute('aria-hidden', 'true')
      el.setAttribute('inert', '')
      ul.insertBefore(el, live[survivors] ?? null)
      collapseOut(el, () => el.remove())
    })
  })
  return listRef
}
