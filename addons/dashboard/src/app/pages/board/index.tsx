import { useMemo, useState } from 'react'
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
import { Lock } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError, STATUSES, type Status, type TicketSummary } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { AddonLanes } from './AddonLane'
import { ListView } from './ListView'
import { TicketCard, TicketCardBody, type BoardPeople } from './TicketCard'
import { Toolbar, type View } from './Toolbar'
import { applyFilters, NO_FILTERS, STATUS_LABEL, type Filters } from './lib'

const DONE_LIMIT = 5

/** Left/right jump to the neighbouring column; up/down nudge. Without this the keyboard moves 25px per press. */
const columnJump: KeyboardCoordinateGetter = (event, { context, currentCoordinates }) => {
  const { droppableRects, droppableContainers, collisionRect } = context
  if (event.code === 'ArrowUp' || event.code === 'ArrowDown') {
    event.preventDefault()
    return { x: currentCoordinates.x, y: currentCoordinates.y + (event.code === 'ArrowUp' ? -40 : 40) }
  }
  if ((event.code !== 'ArrowLeft' && event.code !== 'ArrowRight') || !collisionRect) return undefined
  event.preventDefault()
  const cols = droppableContainers
    .getEnabled()
    .map((c) => ({ id: c.id, rect: droppableRects.get(c.id) }))
    .filter((c): c is { id: typeof c.id; rect: NonNullable<typeof c.rect> } => !!c.rect)
    .sort((a, b) => a.rect.left - b.rect.left)
  if (cols.length === 0) return undefined
  const cx = collisionRect.left + collisionRect.width / 2
  let idx = cols.findIndex((c) => cx >= c.rect.left && cx <= c.rect.left + c.rect.width)
  if (idx < 0) idx = 0
  const next = cols[Math.max(0, Math.min(cols.length - 1, idx + (event.code === 'ArrowRight' ? 1 : -1)))]
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
}) {
  const { setNodeRef, isOver } = useDroppable({ id: status })
  const [showAll, setShowAll] = useState(false)
  const collapsed = status === 'done' && !showAll && tickets.length > DONE_LIMIT
  const visible = collapsed ? tickets.slice(0, DONE_LIMIT) : tickets
  const humanOnly = status === 'done'
  return (
    <section
      aria-label={STATUS_LABEL[status]}
      data-status={status}
      className={cn(
        'flex min-h-0 w-[300px] shrink-0 flex-col rounded-lg border bg-bg/40 transition-colors',
        isOver && draggingFrom !== status ? 'border-brand bg-brand-soft' : 'border-border',
      )}
    >
      <header className="flex items-center gap-2 px-3 py-2">
        <h2 className="text-[13px] font-semibold text-text">{STATUS_LABEL[status]}</h2>
        <span className="rounded-full bg-surface-3 px-1.5 font-mono text-[11px] text-text-muted" aria-label={`${total} tickets`}>
          {total}
        </span>
        <span className="flex-1" />
        {humanOnly && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span tabIndex={0} aria-label="Human-only" className="rounded outline-none focus-visible:ring-2 focus-visible:ring-brand">
                <Lock className="size-3.5 text-text-faint" />
              </span>
            </TooltipTrigger>
            <TooltipContent>Done is reached by a human verdict in Testing.</TooltipContent>
          </Tooltip>
        )}
      </header>
      <div ref={setNodeRef} className="flex min-h-[80px] flex-1 flex-col gap-2 overflow-y-auto px-2 pb-2">
        {visible.length === 0 ? (
          <p className="px-2 py-8 text-center text-[12px] text-text-faint">
            {filtering ? `No ${STATUS_LABEL[status].toLowerCase()} tickets match the filters.` : `No tickets in ${STATUS_LABEL[status].toLowerCase()}.`}
          </p>
        ) : (
          visible.map((t) => <TicketCard key={t.key} ticket={t} people={people} me={me} task={tasks.get(t.key)} canMove onOpen={onOpen} />)
        )}
        {collapsed && (
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
  const byStatus = useMemo(() => {
    const m = new Map<Status, TicketSummary[]>(STATUSES.map((s) => [s, []]))
    for (const t of filtered) m.get(t.status)?.push(t)
    for (const list of m.values()) list.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
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
    mutationFn: ({ key, status }: { key: string; status: Status }) => api.postAction(key, { action: 'set_status', status }),
    onMutate: async ({ key, status }) => {
      await qc.cancelQueries({ queryKey: ticketsKey })
      const prev = qc.getQueryData<TicketSummary[]>(ticketsKey)
      qc.setQueryData<TicketSummary[]>(ticketsKey, (old) => old?.map((t) => (t.key === key ? { ...t, status } : t)))
      return { prev }
    },
    onError: (err, { key, status }, ctx) => {
      qc.setQueryData(ticketsKey, ctx?.prev)
      const e = err instanceof ApiError ? err : null
      toast.error(e?.message ?? `Could not move ${key} to ${STATUS_LABEL[status]}.`, { description: e?.hint })
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ['board'] })
      void qc.invalidateQueries({ queryKey: ['workspaces'] })
      void qc.invalidateQueries({ queryKey: ['today'] })
    },
  })

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: columnJump,
      keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space', 'Enter'] },
    }),
  )

  const open = (key: string) => void navigate({ to: '/ticket/$key', params: { key } })

  function onDragStart(e: DragStartEvent) {
    setDragging((e.active.data.current?.ticket as TicketSummary | undefined) ?? null)
  }
  function onDragEnd(e: DragEndEvent) {
    setDragging(null)
    const t = e.active.data.current?.ticket as TicketSummary | undefined
    const to = e.over?.id as Status | undefined
    if (!t || !to || !STATUSES.includes(to)) return
    const current = qc.getQueryData<TicketSummary[]>(ticketsKey)?.find((x) => x.key === t.key)
    if ((current?.status ?? t.status) === to) return
    move.mutate({ key: t.key, status: to })
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
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
          <div className="flex min-h-0 flex-1 gap-3 overflow-x-auto pb-2">
            {STATUSES.map((s) => (
              <Column
                key={s}
                status={s}
                tickets={byStatus.get(s) ?? []}
                total={(byStatus.get(s) ?? []).length}
                filtering={JSON.stringify(filters) !== JSON.stringify(NO_FILTERS)}
                me={me?.person}
                people={people}
                tasks={tasks}
                onOpen={open}
                draggingFrom={dragging?.status ?? null}
              />
            ))}
            <AddonLanes />
          </div>
          <DragOverlay dropAnimation={null}>
            {dragging ? (
              <div className="w-[284px]">
                <TicketCardBody ticket={dragging} people={people} me={me?.person} task={tasks.get(dragging.key)} overlay />
              </div>
            ) : null}
          </DragOverlay>
        </DndContext>
      )}
    </div>
  )
}
