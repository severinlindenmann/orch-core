import { useEffect, useMemo, useRef, useState } from 'react'
import {
  DndContext,
  DragOverlay,
  KeyboardSensor,
  PointerSensor,
  pointerWithin,
  rectIntersection,
  useDroppable,
  useSensor,
  useSensors,
  type CollisionDetection,
  type DragEndEvent,
  type DragStartEvent,
  type KeyboardCoordinateGetter,
} from '@dnd-kit/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { toast } from 'sonner'
import { can } from '@/api/permissions'
import { useRole } from '@/app/useRole'
import { api } from '@/api/client'
import { STATUSES, type Status, type TicketSummary } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useSlot } from '@/addon-ui'
import { AddonLanes } from './AddonLane'
import { ColumnHeader, ExpandRail } from './ColumnHead'
import { EpicLanes, statusOfDrop } from './EpicLanes'
import { groupByEpic, hasEpics } from './grouping'
import { ListView } from './ListView'
import { TicketCard, TicketCardBody, type BoardPeople } from './TicketCard'
import { Toolbar, type View } from './Toolbar'
import { applyFilters, DONE_LIMIT, NO_FILTERS, STATUS_LABEL, useBoardDisplay, type BoardDisplay, type Filters } from './lib'
import { toastApiError } from '@/app/toast'

/** Left/right jump to the neighbouring column; up/down nudge. Without this the keyboard moves 25px per press. */
const columnJump: KeyboardCoordinateGetter = (event, { context, currentCoordinates }) => {
  const { droppableRects, droppableContainers, collisionRect } = context
  if (event.code === 'ArrowUp' || event.code === 'ArrowDown') {
    event.preventDefault()
    return { x: currentCoordinates.x, y: currentCoordinates.y + (event.code === 'ArrowUp' ? -40 : 40) }
  }
  if ((event.code !== 'ArrowLeft' && event.code !== 'ArrowRight') || !collisionRect) return undefined
  event.preventDefault()
  // The lane grid has several cells per column: pick the column by its left edge, then the cell nearest to the dragged card.
  const cells = droppableContainers
    .getEnabled()
    .map((c) => ({ id: c.id, rect: droppableRects.get(c.id) }))
    .filter((c): c is { id: typeof c.id; rect: NonNullable<typeof c.rect> } => !!c.rect)
  const lefts = [...new Set(cells.map((c) => Math.round(c.rect.left)))].sort((a, b) => a - b)
  if (lefts.length === 0) return undefined
  const cx = collisionRect.left + collisionRect.width / 2
  const colOf = (left: number) => cells.filter((c) => Math.round(c.rect.left) === left)
  let idx = lefts.findIndex((l) => cx >= l && cx <= l + colOf(l)[0].rect.width)
  if (idx < 0) idx = 0
  const column = colOf(lefts[Math.max(0, Math.min(lefts.length - 1, idx + (event.code === 'ArrowRight' ? 1 : -1)))])
  const cy = collisionRect.top + collisionRect.height / 2
  const next = column.reduce((best, c) => {
    const d = (r: typeof c.rect) => (cy < r.top ? r.top - cy : cy > r.top + r.height ? cy - r.top - r.height : 0)
    return d(c.rect) < d(best.rect) ? c : best
  })
  return { x: next.rect.left + 12, y: next.rect.top + 56 }
}

const collision: CollisionDetection = (args) => {
  const hits = pointerWithin(args)
  return hits.length ? hits : rectIntersection(args)
}

function Column({
  status,
  tickets,
  total,
  me,
  people,
  tasks,
  onOpen,
  draggingFrom,
  filtering,
  display,
  collapsedRail,
  canMove,
  onMove,
  onCollapse,
}: {
  status: Status
  tickets: TicketSummary[]
  total: number
  me: string | undefined
  people: BoardPeople
  tasks: Map<string, string>
  onOpen: (key: string) => void
  draggingFrom: Status | null
  filtering: boolean
  display: BoardDisplay
  collapsedRail: boolean
  canMove: boolean
  onMove: (key: string, status: Status) => void
  onCollapse: (collapse: boolean) => void
}) {
  // A collapsed Done rail is not a drop target: Done is reached by a verdict, never by a drop.
  const { setNodeRef, isOver } = useDroppable({ id: status, disabled: collapsedRail && status === 'done' })
  const [showAll, setShowAll] = useState(false)
  const limited = status === 'done' && !showAll && tickets.length > DONE_LIMIT
  const visible = limited ? tickets.slice(0, DONE_LIMIT) : tickets
  if (collapsedRail) {
    return (
      <section
        ref={setNodeRef}
        aria-label={`${STATUS_LABEL[status]} (collapsed)`}
        data-status={status}
        className={cn(
          'flex min-h-0 w-10 flex-col rounded-lg border bg-bg/40 transition-colors',
          isOver && draggingFrom !== status ? 'border-brand bg-brand-soft' : 'border-border',
        )}
      >
        <ExpandRail status={status} total={total} vertical onExpand={() => onCollapse(false)} />
      </section>
    )
  }
  return (
    <section
      aria-label={STATUS_LABEL[status]}
      data-status={status}
      className={cn(
        'flex min-h-0 min-w-0 flex-col rounded-lg border bg-bg/40 transition-colors',
        isOver && draggingFrom !== status ? 'border-brand bg-brand-soft' : 'border-border',
      )}
    >
      <ColumnHeader status={status} tickets={tickets} total={total} onCollapse={() => onCollapse(true)} />
      <div ref={setNodeRef} className="flex min-h-[80px] flex-1 flex-col gap-2 overflow-y-auto px-2 pb-2">
        {visible.length === 0 ? (
          <div className="flex flex-col items-center gap-1 px-2 py-8 text-center text-[12px] text-text-faint">
            <p>Nothing here</p>
            {filtering ? <p>No {STATUS_LABEL[status].toLowerCase()} tickets match the filters.</p> : status === 'backlog' && <p>Create a ticket (c)</p>}
          </div>
        ) : (
          visible.map((t) => <TicketCard key={t.key} ticket={t} people={people} me={me} task={tasks.get(t.key)} canMove={canMove} display={display} onOpen={onOpen} onMove={onMove} />)
        )}
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
    </section>
  )
}

export function BoardPage() {
  usePageHeader('Board')
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const wsId = workspace?.id
  const [view, setView] = useState<View>('board')
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [dragging, setDragging] = useState<TicketSummary | null>(null)
  const [display, setDisplay] = useBoardDisplay()
  const role = useRole()
  const canMove = can(role, 'ticket.move')
  const boardRef = useRef<HTMLDivElement>(null)
  const lanes = useSlot('board.lane')
  const refocus = useRef<{ key: string; status: Status } | null>(null)
  const [overlayWidth, setOverlayWidth] = useState<number | undefined>()

  const { data: me } = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const ticketsKey = ['board', wsId] as const
  const { data: tickets = [], isPending } = useQuery({
    queryKey: ticketsKey,
    queryFn: () => api.listTickets(wsId!),
    enabled: !!wsId,
  })
  const { data: agents = [] } = useQuery({ queryKey: ['agents', wsId], queryFn: () => api.getAgents(wsId!), enabled: !!wsId })

  const people = useMemo<BoardPeople>(() => {
    const byId = new Map(workspace?.members.map((m) => [m.person, m.name]))
    const epics = new Map(tickets.map((t) => [t.key, t.title]))
    return { name: (id) => (id ? (byId.get(id) ?? id) : 'nobody'), epicTitle: (k) => epics.get(k) }
  }, [workspace, tickets])

  const tasks = useMemo(() => new Map(agents.flatMap((a) => a.leases.map((l) => [l.ticket, l.task] as const))), [agents])

  const filtered = useMemo(() => applyFilters(tickets, filters, me?.person), [tickets, filters, me])
  const filtering = JSON.stringify(filters) !== JSON.stringify(NO_FILTERS)
  // Grouped by epic (the default) when this workspace has epics: epics are lane headers, not cards.
  const grouped = display.group === 'epic' && hasEpics(tickets)
  const groups = useMemo(() => groupByEpic(tickets, filtered, filtering), [tickets, filtered, filtering])
  const byStatus = useMemo(() => {
    const m = new Map<Status, TicketSummary[]>(STATUSES.map((s) => [s, []]))
    for (const t of filtered) if (!grouped || t.type !== 'epic') m.get(t.status)?.push(t)
    for (const list of m.values()) list.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    return m
  }, [filtered, grouped])
  // Estimate sums count every ticket of the status, epics included, so they read the same grouped or flat.
  const sumsByStatus = useMemo(() => {
    const m = new Map<Status, TicketSummary[]>(STATUSES.map((s) => [s, []]))
    for (const t of filtered) m.get(t.status)?.push(t)
    return m
  }, [filtered])

  const options = useMemo(
    () => ({
      types: [...new Set(tickets.map((t) => t.type))].sort(),
      labels: [...new Set(tickets.flatMap((t) => t.labels))].sort(),
      people: (workspace?.members ?? []).map((m) => ({ value: m.person, label: m.name })),
      epics: [...new Set(tickets.map((t) => t.parent).filter((p): p is string => !!p))].map((k) => ({
        value: k,
        label: `${k} ${people.epicTitle(k) ?? ''}`.trim(),
      })),
    }),
    [tickets, workspace, people],
  )

  const move = useMutation({
    mutationFn: ({ key, status }: { key: string; status: Status; from?: Status }) => api.postAction(key, { action: 'set_status', status }),
    onSuccess: (_r, { key, status, from }) => {
      if (!from) return
      toast.success(`Moved ${key} to ${STATUS_LABEL[status]}`, {
        duration: 8000,
        action: { label: 'Undo', onClick: () => move.mutate({ key, status: from }) },
      })
    },
    onMutate: async ({ key, status }) => {
      await qc.cancelQueries({ queryKey: ticketsKey })
      const prev = qc.getQueryData<TicketSummary[]>(ticketsKey)
      qc.setQueryData<TicketSummary[]>(ticketsKey, (old) => old?.map((t) => (t.key === key ? { ...t, status } : t)))
      return { prev }
    },
    onError: (err, { key, status }, ctx) => {
      qc.setQueryData(ticketsKey, ctx?.prev)
      toastApiError(err, `Could not move ${key} to ${STATUS_LABEL[status]}.`)
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['board'] })
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      void qc.invalidateQueries({ queryKey: ['today'] })
    },
  })

  // After a move from the menu, keep the keyboard on the card it moved.
  useEffect(() => {
    const r = refocus.current
    if (!r) return
    const el = boardRef.current?.querySelector<HTMLElement>(`[data-status="${r.status}"] [data-testid="card-${r.key}"]`)
    if (el) {
      refocus.current = null
      el.focus()
    }
  }, [tickets])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: columnJump,
      keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space', 'Enter'] },
    }),
  )

  const open = (key: string) => void navigate({ to: '/ticket/$key', params: { key } })

  function onDragStart(e: DragStartEvent) {
    setOverlayWidth(e.active.rect.current.initial?.width)
    setDragging((e.active.data.current?.ticket as TicketSummary | undefined) ?? null)
  }
  function onDragEnd(e: DragEndEvent) {
    setDragging(null)
    const t = e.active.data.current?.ticket as TicketSummary | undefined
    const to = e.over ? statusOfDrop(e.over.id) : undefined
    if (!t || !to) return
    const current = qc.getQueryData<TicketSummary[]>(ticketsKey)?.find((x) => x.key === t.key)
    if ((current?.status ?? t.status) === to) return
    move.mutate({ key: t.key, status: to, from: current?.status ?? t.status })
  }
  function moveFromMenu(key: string, to: Status) {
    const from = qc.getQueryData<TicketSummary[]>(ticketsKey)?.find((x) => x.key === key)?.status
    if (from && from !== to) {
      refocus.current = { key, status: to }
      move.mutate({ key, status: to, from })
    }
  }
  function jump(target: string) {
    const sel = STATUSES.includes(target as Status) ? `[data-status="${target}"]` : `[data-lane="${target}"]`
    boardRef.current?.querySelector<HTMLElement>(sel)?.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' })
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      <h1 className="sr-only">Board</h1>
      <Toolbar
        view={view}
        onView={setView}
        filters={filters}
        onFilters={setFilters}
        types={options.types}
        labels={options.labels}
        people={options.people}
        epics={options.epics}
        shown={filtered.length}
        total={tickets.length}
        display={display}
        onDisplay={setDisplay}
        moveLimit={!role || canMove ? null : role === 'viewer' ? 'viewer' : 'cannot-move'}
        columns={
          view === 'board'
            ? [
                ...STATUSES.map((s) => ({ key: s as string, label: STATUS_LABEL[s], count: byStatus.get(s)?.length ?? 0 })),
                ...lanes.map((l) => ({ key: `${l.addon}/${l.id}`, label: l.title, addon: l.addon })),
              ]
            : null
        }
        onJump={jump}
      />
      {isPending ? (
        <p className="text-[13px] text-text-faint">Loading board…</p>
      ) : view === 'list' ? (
        <ListView tickets={filtered} people={people} onOpen={open} />
      ) : (
        <DndContext
          sensors={sensors}
          collisionDetection={collision}
          onDragStart={onDragStart}
          onDragEnd={onDragEnd}
          onDragCancel={() => setDragging(null)}
        >
          {grouped ? (
            <div ref={boardRef} data-grouped="epic" className="flex min-h-0 min-w-0 flex-1 items-start gap-2 overflow-x-auto overflow-y-auto pb-2">
              <EpicLanes
                groups={groups}
                byStatus={byStatus}
                sumsByStatus={sumsByStatus}
                draggingFrom={dragging?.status ?? null}
                setDisplay={setDisplay}
                people={people}
                me={me?.person}
                tasks={tasks}
                canMove={canMove}
                display={display}
                onOpen={open}
                onMove={moveFromMenu}
              />
            </div>
          ) : (
            <div
              ref={boardRef}
              className="grid min-h-0 min-w-0 flex-1 grid-flow-col gap-2 overflow-x-auto pb-2"
              style={{
                gridTemplateColumns: STATUSES.map((s) => (display.collapsed.includes(s) ? '40px' : 'minmax(216px, 1fr)')).join(' '),
                gridAutoColumns: 'minmax(216px, 1fr)',
              }}
            >
              {STATUSES.map((s) => (
                <Column
                  key={s}
                  status={s}
                  tickets={byStatus.get(s) ?? []}
                  total={(byStatus.get(s) ?? []).length}
                  filtering={filtering}
                  me={me?.person}
                  people={people}
                  tasks={tasks}
                  onOpen={open}
                  draggingFrom={dragging?.status ?? null}
                  display={display}
                  collapsedRail={display.collapsed.includes(s)}
                  canMove={canMove}
                  onMove={moveFromMenu}
                  onCollapse={(c) => setDisplay({ collapsed: c ? [...display.collapsed, s] : display.collapsed.filter((x) => x !== s) })}
                />
              ))}
              <AddonLanes />
            </div>
          )}
          <DragOverlay dropAnimation={null}>
            {dragging ? (
              <div style={{ width: overlayWidth }}>
                <TicketCardBody ticket={dragging} people={people} me={me?.person} task={tasks.get(dragging.key)} display={display} overlay variant={grouped && dragging.parent && groups.lanes.some((l) => l.epic.key === dragging.parent) ? 'lane' : 'card'} />
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      )}
    </div>
  )
}
