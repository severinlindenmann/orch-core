import { ChevronsLeft, ChevronsRight, Lock } from 'lucide-react'
import type { Status, TicketSummary } from '@/api/types'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ColumnSums } from './ColumnSum'
import { STATUS_LABEL } from './lib'

/** Name, count, estimate sums and collapse button of a status column. Shared by the flat board and the lane grid. */
export function ColumnHeader({ status, tickets, total, onCollapse }: { status: Status; tickets: TicketSummary[]; total: number; onCollapse: () => void }) {
  const humanOnly = status === 'done'
  return (
    <header className="@container flex items-center gap-1.5 px-2.5 py-2">
      <h2 className="shrink-0 whitespace-nowrap text-[13px] font-semibold text-text">{STATUS_LABEL[status]}</h2>
      <span className="shrink-0 rounded-full bg-surface-3 px-1.5 font-mono text-[11px] text-text-muted" aria-label={`${total} tickets`}>
        {total}
      </span>
      <ColumnSums tickets={tickets} />
      <span className="min-w-0 flex-1" />
      <button
        type="button"
        aria-label={`Collapse ${STATUS_LABEL[status]}`}
        onClick={onCollapse}
        className="shrink-0 rounded p-0.5 text-text-faint outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
      >
        <ChevronsLeft className="size-3.5" aria-hidden />
      </button>
      {humanOnly && (
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0} role="img" aria-label="Human-only" className="rounded outline-none focus-visible:ring-2 focus-visible:ring-brand">
              <Lock className="size-3.5 text-text-faint" />
            </span>
          </TooltipTrigger>
          <TooltipContent>Done is reached by a human verdict in Testing.</TooltipContent>
        </Tooltip>
      )}
    </header>
  )
}

/** The expand button of a collapsed column. `vertical`: the tall rail of the flat board; otherwise a short header cell. */
export function ExpandRail({ status, total, vertical, onExpand }: { status: Status; total: number; vertical: boolean; onExpand: () => void }) {
  const humanOnly = status === 'done'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`Expand ${STATUS_LABEL[status]}, ${total} tickets`}
          onClick={onExpand}
          className={
            vertical
              ? 'flex h-full min-h-[120px] w-full flex-col items-center gap-2 rounded-lg py-2 text-text-muted outline-none hover:bg-accent hover:text-text focus-visible:ring-2 focus-visible:ring-brand'
              : 'flex h-full min-h-9 w-full flex-col items-center justify-center gap-0.5 rounded-lg py-1 text-text-muted outline-none hover:bg-accent hover:text-text focus-visible:ring-2 focus-visible:ring-brand'
          }
        >
          <ChevronsRight className="size-3.5" aria-hidden />
          <span className="rounded-full bg-surface-3 px-1.5 font-mono text-[11px]">{total}</span>
          {vertical && <span className="text-[13px] font-semibold [writing-mode:vertical-rl]">{STATUS_LABEL[status]}</span>}
          {vertical && humanOnly && <Lock className="size-3 text-text-faint" aria-hidden />}
        </button>
      </TooltipTrigger>
      <TooltipContent side={vertical ? 'left' : 'bottom'}>{humanOnly ? `${STATUS_LABEL[status]}. Human-only: Done is reached by a verdict in Testing.` : STATUS_LABEL[status]}</TooltipContent>
    </Tooltip>
  )
}
