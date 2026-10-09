import { useEffect, useMemo, useState } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { Eye } from 'lucide-react'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import type { AddonDecision, NeedsYouItem, TicketDocument } from '@/api/types'
import { useAddons } from '@/addon-ui'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../../workspace'
import { useRole } from '../../useRole'
import { useAttention, type Attention } from '../../attention'
import { usePageHeader } from '../../shell/ShellUi'
import { SignDialog } from '../ticket/SignDialog'
import type { HumanAction } from '../ticket/shared'
import { QueueGroup } from './groups'
import { acceptOrder, buildGroups, foldLabel, pruneOrder, reconcile, toEntries, type Entry, type Row } from './queue'
import { ReloginGroup } from './relogin'
import { ApprovalRow, DecisionRow, FoldRow, QuestionRow, VerdictRow } from './rows'
import { AgentsBar, AgentsPanel, Glance, GLANCE_TILES, Recently } from './side'
import { displayName, useMediaQuery, WIDE_QUERY, type Directory } from './shared'

function dateLine(now: string) {
  return new Date(now).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

export function TodayPage() {
  usePageHeader('Today')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const agentsQ = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const decisionsQ = useQuery({ queryKey: ['addon-decisions', ws], queryFn: () => api.getAddonDecisions(ws!), enabled: !!ws })
  // Mock only: switching the demo dataset is a fresh start for the queue, not a wave of "new" items. The queue waits
  // until Today and the decisions have been read after the switch, so it never starts from the other dataset's items.
  const dataset = useQuery({ queryKey: ['dev-dataset'], queryFn: () => api.getDataset() })
  const ds = dataset.data?.dataset
  const [switched, setSwitched] = useState<{ ds?: string; at: number }>({ ds, at: 0 })
  if (switched.ds !== ds) setSwitched({ ds, at: switched.ds === undefined ? 0 : dataset.dataUpdatedAt })
  const stale = today.dataUpdatedAt < switched.at || decisionsQ.dataUpdatedAt < switched.at
  const { refetch: refetchToday } = today
  const { refetch: refetchDecisions } = decisionsQ
  useEffect(() => {
    if (!stale) return
    void refetchToday()
    void refetchDecisions()
  }, [stale, refetchToday, refetchDecisions])
  const attention = useAttention(ws)
  const role = useRole()
  const readOnly = !can(role, 'ticket.act')

  if (!today.data || !agentsQ.data || !me.data || !decisionsQ.data || !role || dataset.isPending || stale) {
    return (
      <div className="space-y-4" aria-busy="true">
        <h1 className="text-xl font-semibold tracking-tight">Today</h1>
        <Skeleton className="h-5 w-96" />
        <Skeleton className="h-40 w-full max-w-3xl" />
      </div>
    )
  }
  // A new workspace or person starts a new queue (its own accepted order and open row).
  return (
    <TodayInbox
      key={`${ws}:${me.data.person}:${readOnly}:${ds}`}
      items={readOnly ? today.data.read_only_open : today.data.needs_you}
      decisions={decisionsQ.data}
      readOnly={readOnly}
      canAddon={can(role, 'addon.action')}
      viewer={me.data.person}
      attention={attention}
    />
  )
}

function TodayInbox({ items, decisions, readOnly, canAddon, viewer, attention }: {
  items: NeedsYouItem[]
  decisions: AddonDecision[]
  readOnly: boolean
  canAddon: boolean
  viewer: string
  attention: Attention
}) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const agentsQ = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })
  const addons = useAddons()
  const wide = useMediaQuery(WIDE_QUERY)
  const now = today.data!.now
  const sessions = useMemo(() => agentsQ.data ?? [], [agentsQ.data])
  const dir: Directory = { workspace, agents: sessions }

  const entries = useMemo(() => toEntries(items, decisions), [items, decisions])
  // The order on screen changes only on load and on "Show new": arrivals wait in `fresh`, nothing moves under the pointer.
  const [order, setOrder] = useState<string[]>(() => acceptOrder(entries))
  const { shown, fresh } = useMemo(() => reconcile(order, entries), [order, entries])
  // With nothing on screen there is nothing to keep still: take the new items in directly. Resolved ids leave the
  // accepted order, so an item that reopens later comes back through the pill, not in its old place.
  useEffect(() => {
    if (shown.length === 0 && fresh.length > 0) setOrder(acceptOrder(entries))
    else setOrder((o) => pruneOrder(o, entries))
  }, [shown.length, fresh.length, entries])
  const groups = useMemo(() => buildGroups(shown), [shown])

  // Exactly one open row: on load the first blocking question, else the first row.
  const [expanded, setExpanded] = useState<string | null>(() => {
    if (readOnly) return null
    const first = buildGroups(reconcile(order, entries).shown)
    const blockingQ = first.find((g) => g.id === 'questions')?.rows.find((r) => r.kind === 'one' && r.entry.blocking)
    return blockingQ?.id ?? first[0]?.rows[0]?.id ?? null
  })
  const toggle = (id: string) => setExpanded((e) => (e === id ? null : id))

  const [signing, setSigning] = useState<{ ticket: string; action: HumanAction } | null>(null)
  const sign = (ticket: string, action: HumanAction) => setSigning({ ticket, action })

  const claimTickets = sessions.flatMap((a) => a.claims.map((c) => c.ticket))
  const keys = [...new Set([...entries.flatMap((e) => (e.group === 'addons' ? (e.decision.ticket ? [e.decision.ticket] : []) : [e.item.ticket])), ...claimTickets])]
  const ticketQs = useQueries({ queries: keys.map((k) => ({ queryKey: ['ticket', k], queryFn: () => api.getTicket(k) })) })
  const byKey: Record<string, TicketDocument | undefined> = {}
  keys.forEach((k, i) => (byKey[k] = ticketQs[i]?.data))

  const addonTitle = (name: string) => addons.data?.find((a) => a.name === name)?.title ?? name
  const owners = workspace?.members.filter((m) => m.role === 'owner').map((m) => m.person) ?? []
  const fallbackDecider = attention.waitingOnOthers.who.map((p) => displayName(dir, p)).join(', ') || 'The owner'
  const deciderOf = (e: Entry): string => {
    if (e.group === 'addons') return owners.map((p) => displayName(dir, p)).join(', ') || fallbackDecider
    const t = byKey[e.item.ticket]
    const to = e.item.kind === 'question' ? t?.questions_state.find((q) => q.id === e.item.ref)?.to : undefined
    const person = to?.startsWith('p_') ? to : t?.people.owner
    return person ? displayName(dir, person) : fallbackDecider
  }

  const renderRow = (row: Row) => {
    if (row.kind === 'fold') {
      const ds = row.entries.map((e) => e.decision)
      const title = addonTitle(row.addon)
      return (
        <FoldRow
          key={row.id}
          label={foldLabel(ds.length, title, ds.map((d) => d.title))}
          decisions={ds}
          addonTitle={title}
          ticketTitle={(k) => byKey[k]?.title}
          decider={!canAddon ? deciderOf(row.entries[0]) : undefined}
          readOnly={!canAddon}
          expanded={expanded === row.id}
          onToggle={() => toggle(row.id)}
        />
      )
    }
    const e = row.entry
    if (e.group === 'addons') {
      const d = e.decision
      return (
        <DecisionRow
          key={row.id}
          d={d}
          readOnly={!canAddon}
          addonTitle={addonTitle(d.addon)}
          ticketTitle={d.ticket ? byKey[d.ticket]?.title : undefined}
          expanded={expanded === row.id}
          onToggle={() => toggle(row.id)}
          decider={!canAddon ? deciderOf(e) : undefined}
        />
      )
    }
    const askedBy = e.item.kind === 'question' ? byKey[e.item.ticket]?.questions_state.find((q) => q.id === e.item.ref)?.asked_by : undefined
    const props = { item: e.item, ticket: byKey[e.item.ticket], askedBy: askedBy ? displayName(dir, askedBy) : undefined, now, expanded: expanded === row.id, onToggle: () => toggle(row.id), sign, decider: readOnly ? deciderOf(e) : undefined }
    if (e.item.kind === 'question') return <QuestionRow key={row.id} {...props} />
    if (e.item.kind === 'approval') return <ApprovalRow key={row.id} {...props} />
    return <VerdictRow key={row.id} {...props} />
  }

  const n = attention.needsYou.total
  const counts = attention.agents
  const waiting = attention.waitingOnOthers
  const whoWaits = waiting.who.map((p) => displayName(dir, p)).join(', ')
  const allOwners = waiting.who.length > 0 && waiting.who.every((p) => owners.includes(p))
  const scope = `${ws}:${viewer}`

  const header = (
    <div className="space-y-1">
      <h1 className="text-xl font-semibold tracking-tight">Today</h1>
      <p className="flex flex-wrap items-center gap-2 text-[13px] tabular-nums text-text-muted">
        <span>
          {readOnly
            ? `${dateLine(now)} · Nothing needs you · ${waiting.count} open in the workspace`
            : `${dateLine(now)} · ${n} need you · ${plural(counts.working, 'agent', 'agents')} working`}
        </span>
        {readOnly && (
          <span className="inline-flex items-center gap-1 rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-text-muted">
            <Eye className="size-3" />
            viewer · read only
          </span>
        )}
      </p>
    </div>
  )

  const agentProps = { sessions, viewer, counts, tickets: byKey, now }

  const queue = (
    <section aria-label={readOnly ? 'Open in the workspace' : 'Needs you'} className="min-w-0 space-y-3">
      {/* A zero-height sticky slot: the pill floats over the top of the queue, so its arrival moves no row. top-7 less the pill's -mt-5 leaves it 8 px below the scroll edge when stuck. */}
      <div data-testid="new-items" className="pointer-events-none sticky top-7 z-10 mb-0 flex h-0 justify-center">
        {fresh.length > 0 && (
          <button
            type="button"
            onClick={() => setOrder(acceptOrder(entries))}
            className="orch-pill-in pointer-events-auto -mt-5 flex h-7 items-center gap-1.5 rounded-full border border-border bg-surface-2 px-3 text-xs text-text shadow-md outline-none hover:bg-surface focus-visible:ring-2 focus-visible:ring-ring"
          >
            {fresh.length} new · Show
          </button>
        )}
      </div>
      <ReloginGroup now={now} />
      {groups.map((g) => (
        <QueueGroup
          key={g.id}
          group={g}
          now={now}
          scope={scope}
          note={g.id === 'addons' ? 'you sign every answer in orch' : undefined}
          renderRow={renderRow}
          pinned={expanded}
        />
      ))}
      {groups.length === 0 && (
        <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center">
          <p className="text-[15px] font-medium text-text">{readOnly ? 'Nothing is open in the workspace' : 'Nothing needs you right now'}</p>
          <p className="mt-1 text-[13px] text-text-muted">
            {counts.working > 0
              ? `${plural(counts.working, 'agent is', 'agents are')} working. You will see a row here when one needs a decision.`
              : 'No agent is working either. Pick a ticket from the board to start something.'}
          </p>
        </div>
      )}
      {!readOnly && waiting.count > 0 && (
        <p className="px-1 text-xs text-text-muted">
          {waiting.count} more {waiting.count === 1 ? 'is' : 'are'} waiting {allOwners ? `for an owner (${whoWaits})` : `for ${whoWaits || 'other people'}`}.
        </p>
      )}
    </section>
  )

  const signTicket = signing ? byKey[signing.ticket] : undefined

  return (
    <div className="space-y-5">
      {header}
      {wide ? (
        <div className="grid grid-cols-[minmax(0,1fr)_320px] gap-6">
          {queue}
          <aside className="min-w-0 space-y-4">
            <AgentsPanel {...agentProps} />
            <Glance readOnly={!canAddon} max={GLANCE_TILES} />
            <Recently recent={today.data!.recent} now={now} />
          </aside>
        </div>
      ) : (
        <div className="space-y-4">
          <AgentsBar {...agentProps} />
          {queue}
          <Glance readOnly={!canAddon} />
        </div>
      )}
      {signTicket && <SignDialog ticket={signTicket} action={signing!.action} onClose={() => setSigning(null)} />}
    </div>
  )
}
