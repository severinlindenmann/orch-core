import { Fragment, useState } from 'react'
import { useDroppable } from '@dnd-kit/core'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { STATUSES, type Status, type TicketSummary } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { AddonLanes } from './AddonLane'
import { ColumnHeader, ExpandRail } from './ColumnHead'
import { ColumnSums } from './ColumnSum'
import { isCollapsed, NO_EPIC, progressLabel, type EpicGroups, type EpicLane } from './grouping'
import { DONE_LIMIT, STATUS_LABEL, TypeIcon, type BoardDisplay } from './lib'
import { CardFields, TicketCard, type BoardPeople } from './TicketCard'

/** What a lane cell is called as a drop target: one per lane and status, so ids stay unique. */
export const cellId = (lane: string, status: Status) => `${lane}|${status}`
/** The status a droppable id stands for: a flat column ("backlog") or a lane cell ("DEMO-0040|backlog"). */
export const statusOfDrop = (id: string | number): Status | undefined => {
  const s = String(id).split('|').pop() as Status
  return STATUSES.includes(s) ? s : undefined
}

interface Shared {
  people: BoardPeople
  me: string | undefined
  tasks: Map<string, string>
  canMove: boolean
  display: BoardDisplay
  onOpen: (key: string) => void
  onMove: (key: string, status: Status) => void
}

function HeaderCell({
  status,
  tickets,
  sumOf,
  collapsed,
  draggingFrom,
  onCollapse,
}: {
  status: Status
  tickets: TicketSummary[]
  /** What the estimate sums add up: the cards plus the epics of this status. */
  sumOf: TicketSummary[]
  collapsed: boolean
  draggingFrom: Status | null
  onCollapse: (c: boolean) => void
}) {
  // Dropping on the header sets the status too; Done stays a verdict, so its collapsed header is no target.
  const { setNodeRef, isOver } = useDroppable({ id: status, disabled: collapsed && status === 'done' })
  return (
    <div
      ref={setNodeRef}
      role="region"
      aria-label={collapsed ? `${STATUS_LABEL[status]} (collapsed)` : STATUS_LABEL[status]}
      data-status={status}
      className={cn(
        'sticky top-0 z-10 rounded-lg border bg-bg transition-colors',
        isOver && draggingFrom !== status ? 'border-brand bg-brand-soft' : 'border-border',
      )}
    >
      {collapsed ? (
        <ExpandRail status={status} total={tickets.length} vertical={false} onExpand={() => onCollapse(false)} />
      ) : (
        <ColumnHeader status={status} tickets={tickets} sumOf={sumOf} total={tickets.length} onCollapse={() => onCollapse(true)} />
      )}
    </div>
  )
}

function Cell({
  laneKey,
  laneLabel,
  status,
  tickets,
  collapsedRail,
  draggingFrom,
  variant,
  shared,
}: {
  laneKey: string
  laneLabel: string
  status: Status
  tickets: TicketSummary[]
  collapsedRail: boolean
  draggingFrom: Status | null
  variant: 'card' | 'lane'
  shared: Shared
}) {
  const { setNodeRef, isOver } = useDroppable({ id: cellId(laneKey, status), disabled: collapsedRail && status === 'done' })
  const [showAll, setShowAll] = useState(false)
  if (collapsedRail) {
    return (
      <div ref={setNodeRef} data-status={status} className="pt-1 text-center font-mono text-[11px] text-text-faint" aria-label={`${laneLabel}: ${tickets.length} ${STATUS_LABEL[status]}`}>
        {tickets.length > 0 ? tickets.length : ''}
      </div>
    )
  }
  const limited = status === 'done' && !showAll && tickets.length > DONE_LIMIT
  const visible = limited ? tickets.slice(0, DONE_LIMIT) : tickets
  return (
    <div
      ref={setNodeRef}
      role="group"
      aria-label={`${laneLabel} · ${STATUS_LABEL[status]}`}
      data-status={status}
      data-lane-cell={laneKey}
      className={cn(
        'flex min-h-9 min-w-0 flex-col rounded-lg border border-transparent p-1 transition-colors',
        variant === 'lane' ? 'gap-1' : 'gap-2',
        isOver && draggingFrom !== status && 'border-brand bg-brand-soft',
      )}
    >
      {visible.map((t) => (
        <TicketCard key={t.key} ticket={t} people={shared.people} me={shared.me} task={shared.tasks.get(t.key)} canMove={shared.canMove} display={shared.display} variant={variant} onOpen={shared.onOpen} onMove={shared.onMove} />
      ))}
      {limited && (
        <Button variant="ghost" size="sm" className="h-7 text-[12px] text-text-muted" onClick={() => setShowAll(true)}>
          Show all {tickets.length}
        </Button>
      )}
      {status === 'done' && showAll && tickets.length > DONE_LIMIT && (
        <Button variant="ghost" size="sm" className="h-7 text-[12px] text-text-muted" onClick={() => setShowAll(false)}>
          Show last {DONE_LIMIT}
        </Button>
      )}
    </div>
  )
}

function LaneHeader({
  title,
  epic,
  progress,
  count,
  tickets,
  collapsed,
  canToggle,
  emptyNote,
  onToggle,
  onOpen,
}: {
  title: string
  epic?: TicketSummary
  progress?: EpicLane
  count: number
  tickets: TicketSummary[]
  collapsed: boolean
  canToggle: boolean
  emptyNote?: string
  onToggle: (collapse: boolean) => void
  onOpen: (key: string) => void
}) {
  const Chevron = collapsed ? ChevronRight : ChevronDown
  const name = epic ? `${epic.key} ${epic.title}` : title
  return (
    <div className="col-span-full mt-1 rounded-lg border border-border bg-surface-2">
      <div className="sticky left-0 flex w-max max-w-full items-center gap-2 px-2 py-1.5">
        {canToggle ? (
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-label={`${collapsed ? 'Expand' : 'Collapse'} ${name}`}
            onClick={() => onToggle(!collapsed)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowLeft' && !collapsed) {
                e.preventDefault()
                onToggle(true)
              } else if (e.key === 'ArrowRight' && collapsed) {
                e.preventDefault()
                onToggle(false)
              }
            }}
            className="rounded p-0.5 text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
          >
            <Chevron className="size-4" aria-hidden />
          </button>
        ) : (
          <span className="size-5" aria-hidden />
        )}
        {epic ? (
          <>
            <TypeIcon type="epic" />
            <button
              type="button"
              onClick={() => onOpen(epic.key)}
              aria-label={`Open ${epic.key}`}
              className="rounded font-mono text-[12px] font-semibold text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand"
            >
              {epic.key}
            </button>
            <span className="max-w-[420px] truncate text-[13px] font-semibold text-text" title={epic.title}>
              {epic.title}
            </span>
          </>
        ) : (
          <span className="text-[13px] font-semibold text-text">{title}</span>
        )}
        {progress && (
          <>
            <span className="font-mono text-[11px] text-text-muted">{progressLabel(progress)}</span>
            <div role="img" aria-label={`${name}: ${progressLabel(progress)}`} className="h-1 w-20 overflow-hidden rounded-full bg-surface-3"
            >
              <div className={cn('h-full rounded-full', progress.total > 0 && progress.done === progress.total ? 'bg-success' : 'bg-brand')} style={{ width: `${progress.total ? (progress.done / progress.total) * 100 : 0}%` }} />
            </div>
          </>
        )}
        <span className="rounded-full bg-surface-3 px-1.5 font-mono text-[11px] text-text-muted" aria-label={`${count} tickets shown`}>
          {count}
        </span>
        {emptyNote && <span className="text-[12px] text-text-faint">{emptyNote}</span>}
        <ColumnSums tickets={tickets} />
        {epic && (
          <span title="The epic's own estimate" className="inline-flex items-center gap-1">
            <CardFields ticket={epic} />
          </span>
        )}
      </div>
    </div>
  )
}

/**
 * The grouped board: one status header row, then one swimlane per epic (children as two-line cards) and a final
 * "No epic" lane. Cells are drop targets for their status; dropping into another lane changes the status only.
 */
export function EpicLanes({
  groups,
  byStatus,
  sumsByStatus,
  draggingFrom,
  setDisplay,
  ...shared
}: Shared & {
  groups: EpicGroups
  /** Every card on the board per status (all lanes, folded or not): the header counts and estimate sums. */
  byStatus: Map<Status, TicketSummary[]>
  sumsByStatus: Map<Status, TicketSummary[]>
  draggingFrom: Status | null
  setDisplay: (patch: Partial<BoardDisplay>) => void
}) {
  const { display } = shared
  const railed = (s: Status) => display.collapsed.includes(s)
  const setLane = (key: string, collapse: boolean) => setDisplay({ lanes: { ...display.lanes, [key]: collapse } })
  const split = (tickets: TicketSummary[]) => {
    const m = new Map<Status, TicketSummary[]>(STATUSES.map((s) => [s, []]))
    for (const t of tickets) m.get(t.status)?.push(t)
    for (const list of m.values()) list.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    return m
  }
  const cols = STATUSES.map((s) => (railed(s) ? '40px' : 'minmax(216px, 1fr)')).join(' ')
  const minWidth = STATUSES.reduce((n, s) => n + (railed(s) ? 40 : 216), 0) + (STATUSES.length - 1) * 8
  const lanes = [
    ...groups.lanes.map((l) => ({ key: l.epic.key, lane: l as EpicLane | null, tickets: l.children })),
    ...(groups.none.length > 0 || groups.lanes.length === 0 ? [{ key: NO_EPIC, lane: null, tickets: groups.none }] : []),
  ]
  return (
    <>
      <div className="grid content-start gap-x-2 gap-y-1" style={{ gridTemplateColumns: cols, minWidth }}>
        {STATUSES.map((s) => (
          <HeaderCell
            key={s}
            status={s}
            tickets={byStatus.get(s) ?? []}
            sumOf={sumsByStatus.get(s) ?? []}
            collapsed={railed(s)}
            draggingFrom={draggingFrom}
            onCollapse={(c) => setDisplay({ collapsed: c ? [...display.collapsed, s] : display.collapsed.filter((x) => x !== s) })}
          />
        ))}
        {lanes.map(({ key, lane, tickets }) => {
          const none = key === NO_EPIC
          const collapsed = tickets.length === 0 ? true : lane ? isCollapsed(lane, display.lanes) : (display.lanes[NO_EPIC] ?? false)
          const cells = split(tickets)
          const label = none ? 'No epic' : lane!.epic.key
          return (
            <Fragment key={key}>
              <LaneHeader
                title="No epic"
                epic={lane?.epic}
                progress={lane ?? undefined}
                count={tickets.length}
                tickets={tickets}
                collapsed={collapsed}
                canToggle={tickets.length > 0}
                emptyNote={tickets.length === 0 ? (lane && lane.total > 0 ? 'No child matches the filters' : 'No child tickets') : undefined}
                onToggle={(c) => setLane(key, c)}
                onOpen={shared.onOpen}
              />
              {!collapsed &&
                STATUSES.map((s) => (
                  <Cell
                    key={s}
                    laneKey={key}
                    laneLabel={label}
                    status={s}
                    tickets={cells.get(s) ?? []}
                    collapsedRail={railed(s)}
                    draggingFrom={draggingFrom}
                    variant={none ? 'card' : 'lane'}
                    shared={shared}
                  />
                ))}
            </Fragment>
          )
        })}
      </div>
      <div className="sticky top-0 grid max-h-[calc(100vh-14rem)] grid-flow-col auto-cols-[minmax(216px,1fr)] gap-2 self-start empty:hidden">
        <AddonLanes />
      </div>
    </>
  )
}
