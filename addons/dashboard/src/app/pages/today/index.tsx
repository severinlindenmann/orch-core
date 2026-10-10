import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { ChevronDown, Eye } from 'lucide-react'
import { can } from '@/api/permissions'
import type { AddonDecision, NeedsYouItem, TicketDocument } from '@/api/types'
import { useAddons, useSlot } from '@/addon-ui'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../../workspace'
import { useRole } from '../../useRole'
import { useAttention, type Attention } from '../../attention'
import { SEEN_PREFIX, useTodayGeneration } from '../../todayRestart'
import { usePageHeader } from '../../shell/ShellUi'
import { TodaySkeleton } from '../skeletons'
import { LoadFailed } from '@/components/LoadFailed'
import { LOADER_WAIT_MS } from '../../routeData'
import { useLoadFailure, useWaitAtMost } from '../../useLoadFailure'
import { useConnections } from '../settings/connectionUi'
import { SignDialog } from '../ticket/SignDialog'
import type { HumanAction } from '../ticket/shared'
import { QueueGroup } from './groups'
import { acceptOrder, buildGroups, foldLabel, pruneOrder, reconcile, toEntries, type Entry, type Row } from './queue'
import { ReloginGroup } from './relogin'
import { ApprovalRow, DecisionRow, FoldRow, NewItemContext, SigningContext, QuestionRow, VerdictRow } from './rows'
import { AgentsBar, AgentsPanel, Glance, GLANCE_TILES, Recently } from './side'
import { displayName, useMediaQuery, useSessionState, WIDE_QUERY, type Directory } from './shared'
import { queries } from '@/api/queries'

function dateLine(now: string) {
  return new Date(now).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

function readSeen(key: string): Set<string> | null {
  try {
    const value = localStorage.getItem(key)
    return value ? new Set(JSON.parse(value) as string[]) : null
  } catch {
    return null
  }
}

function writeSeen(key: string, ids: string[]) {
  try {
    localStorage.setItem(key, JSON.stringify(ids))
  } catch {
    /* storage unavailable: nothing is marked new next time */
  }
}

/** Which open item a signing action is for: the question id, the gate, or `verify` for a verdict. */
function actionRef(a: HumanAction): string {
  if (a.kind === 'answer') return a.question
  if (a.kind === 'verdict') return 'verify'
  return a.gate
}

/** A viewer's summary line for one person who decides: "Severin decides · 4 open", opening into that person's rows. */
function PersonSummary({ person, entries, scope, renderRow }: { person: string; entries: Entry[]; scope: string; renderRow: (row: Row) => ReactNode }) {
  const [open, setOpen] = useSessionState(`orch.today.person.${scope}.${person}`, false)
  const kinds = buildGroups(entries).map((g) => `${g.count} ${g.label.toLowerCase()}`).join(' · ')
  return (
    <section aria-label={`${person} decides`} className="overflow-hidden rounded-lg border border-border bg-surface">
      <h2>
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center gap-1 px-3 py-3 text-left text-sm outline-none hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
        >
          <ChevronDown className={cn('mr-1 size-4 shrink-0 text-text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
          <span className="font-semibold text-text">{person} decides</span>{' '}
          <span className="min-w-0 truncate tabular-nums text-text-muted">· {entries.length} open · {kinds}</span>
        </button>
      </h2>
      {open && <ul className="border-t border-border">{buildGroups(entries).flatMap((g) => g.rows).map(renderRow)}</ul>}
    </section>
  )
}

export function TodayPage() {
  usePageHeader('Today')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const today = useQuery({ ...queries.today(ws!), enabled: !!ws })
  const agentsQ = useQuery({ ...queries.agents(ws!), enabled: !!ws })
  const me = useQuery(queries.me())
  const decisionsQ = useQuery({ ...queries.addonDecisions(ws!), enabled: !!ws })
  // Mock only: switching the demo dataset is a fresh start for the queue, not a wave of "new" items. The queue waits
  // until Today and the decisions have been read after the switch, so it never starts from the other dataset's items.
  const dataset = useQuery(queries.devDataset())
  const generation = useTodayGeneration()
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

  // The Glance is part of the first screen: Today waits for its addon states too (at most LOADER_WAIT_MS, then it
  // shows with the Glance's own placeholders), so nothing appears beside the queue a moment later.
  // Also while the addons themselves are not known yet (no Glance items to wait for so far).
  const addonList = useAddons()
  const glancePending = useSlot('today.card').some((c) => c.waiting?.status === 'pending')
  const glanceWaiting = useWaitAtMost((addonList.isPending && !addonList.isError) || glancePending, LOADER_WAIT_MS)
  // The owner's connections are part of Today's first screen (attention.ready): a failure there says so too.
  const connectionsQ = useConnections(can(role, 'settings') ? ws : undefined)
  const failure = useLoadFailure(today, agentsQ, decisionsQ, connectionsQ)

  if (failure.failed) return <LoadFailed what="Today" onRetry={failure.retry} />
  if (!today.data || !agentsQ.data || !me.data || !decisionsQ.data || !role || dataset.isPending || stale || !attention.ready || glanceWaiting) {
    return <TodaySkeleton inPage />
  }
  // A new workspace or person starts a new queue (its own accepted order and open row).
  return (
    <TodayInbox
      key={`${ws}:${me.data.person}:${readOnly}:${ds}:${generation}`}
      generation={`${ds}:${generation}`}
      items={readOnly ? today.data.read_only_open : today.data.needs_you}
      decisions={decisionsQ.data}
      readOnly={readOnly}
      canAddon={can(role, 'addon.action')}
      viewer={me.data.person}
      attention={attention}
    />
  )
}

function TodayInbox({ items, decisions, readOnly, canAddon, viewer, attention, generation }: {
  generation: string
  items: NeedsYouItem[]
  decisions: AddonDecision[]
  readOnly: boolean
  canAddon: boolean
  viewer: string
  attention: Attention
}) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const today = useQuery({ ...queries.today(ws!), enabled: !!ws })
  const agentsQ = useQuery({ ...queries.agents(ws!), enabled: !!ws })
  const addons = useAddons()
  const wide = useMediaQuery(WIDE_QUERY)
  const now = today.data!.now
  const sessions = useMemo(() => agentsQ.data ?? [], [agentsQ.data])
  const dir: Directory = { workspace, agents: sessions }

  const entries = useMemo(() => toEntries(items, decisions), [items, decisions])
  // The order on screen changes only on load and on "Show new": arrivals wait in `fresh`, nothing moves under the pointer.
  // "New" means new since the person's last look: ids not in the list they last saw here (kept per workspace and
  // person in the browser; a first visit, a dataset switch or Reset marks nothing new). New items lead their group.
  const seenKey = `${SEEN_PREFIX}${ws}:${viewer}`
  const [newIds, setNewIds] = useState<ReadonlySet<string>>(() => {
    const seen = readSeen(seenKey)
    return new Set(seen ? entries.filter((e) => !seen.has(e.id)).map((e) => e.id) : [])
  })
  const [order, setOrder] = useState<string[]>(() => acceptOrder(entries, newIds))
  useEffect(() => writeSeen(seenKey, order), [seenKey, order])
  const { shown, fresh } = useMemo(() => reconcile(order, entries), [order, entries])
  // Taking arrivals in ("N new · Show", or directly when nothing is on screen) marks them new and opens the groups they
  // landed in (only those: another group the person closed stays closed).
  const [reveal, setReveal] = useState<{ n: number; groups: string[] }>({ n: 0, groups: [] })
  const takeFresh = useCallback(() => {
    const ids = new Set([...newIds, ...fresh.map((e) => e.id)])
    setNewIds(ids)
    setOrder(acceptOrder(entries, ids))
    setReveal((r) => ({ n: r.n + 1, groups: [...new Set(fresh.map((e) => e.group))] }))
  }, [newIds, fresh, entries])
  // With nothing on screen there is nothing to keep still: take the new items in directly. Resolved ids leave the
  // accepted order, so an item that reopens later comes back through the pill, not in its old place.
  useEffect(() => {
    if (shown.length === 0 && fresh.length > 0) takeFresh()
    else setOrder((o) => pruneOrder(o, entries))
  }, [shown.length, fresh.length, entries, takeFresh])
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
  // From the touch until the host confirms, the row being signed is dimmed with "Signing…" (R-d).
  const [pending, setPending] = useState(false)
  const sign = (ticket: string, action: HumanAction) => setSigning({ ticket, action })

  const claimTickets = sessions.flatMap((a) => a.claims.map((c) => c.ticket))
  const keys = [...new Set([...entries.flatMap((e) => (e.group === 'addons' ? (e.decision.ticket ? [e.decision.ticket] : []) : [e.item.ticket])), ...claimTickets])]
  const ticketQs = useQueries({ queries: keys.map((k) => queries.ticket(k)) })
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

  const renderContent = (row: Row) => {
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

  const renderRow = (row: Row) => {
    const ids = row.kind === 'one' ? [row.entry.id] : row.entries.map((e) => e.id)
    const e = row.kind === 'one' ? row.entry : undefined
    const beingSigned = pending && !!signing && !!e && e.group !== 'addons' && signing.ticket === e.item.ticket && actionRef(signing.action) === e.item.ref
    return (
      <NewItemContext.Provider key={row.id} value={ids.some((id) => newIds.has(id))}>
        <SigningContext.Provider value={beingSigned}>{renderContent(row)}</SigningContext.Provider>
      </NewItemContext.Provider>
    )
  }

  // A viewer decides nothing: the open items are summed up per person who decides, each opening on demand.
  const byPerson = new Map<string, Entry[]>()
  if (readOnly) for (const e of shown) byPerson.set(deciderOf(e), [...(byPerson.get(deciderOf(e)) ?? []), e])

  const n = attention.needsYou.total
  const counts = attention.agents
  const waiting = attention.waitingOnOthers
  const whoWaits = waiting.who.map((p) => displayName(dir, p)).join(', ')
  const allOwners = waiting.who.length > 0 && waiting.who.every((p) => owners.includes(p))
  const scope = `${ws}:${viewer}:${generation}`

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
      {/* A zero-height sticky slot, first in the queue: the pill hangs down into the first group's header line (never up
          over the Agents row above the queue), so its arrival moves no row. It stays in view while scrolling. */}
      <div data-testid="new-items" className="pointer-events-none sticky top-2 z-10 mb-0 flex h-0 justify-center">
        {fresh.length > 0 && (
          <button
            type="button"
            onClick={takeFresh}
            className="orch-pill-in pointer-events-auto mt-2 flex h-7 items-center gap-1.5 rounded-full border border-border bg-surface-2 px-3 text-xs text-text shadow-md outline-none hover:bg-surface focus-visible:ring-2 focus-visible:ring-ring"
          >
            {fresh.length} new · Show
          </button>
        )}
      </div>
      <ReloginGroup now={now} />
      {readOnly ? [...byPerson].map(([person, personEntries]) => (
        <PersonSummary key={person} person={person} entries={personEntries} scope={scope} renderRow={renderRow} />
      )) : groups.map((g) => (
        <QueueGroup
          key={g.id}
          group={g}
          now={now}
          scope={scope}
          note={g.id === 'addons' ? 'you sign every answer in orch' : undefined}
          renderRow={renderRow}
          pinned={expanded}
          reveal={reveal.groups.includes(g.id) ? reveal.n : 0}
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
      {signTicket && <SignDialog ticket={signTicket} action={signing!.action} onClose={() => setSigning(null)} onPending={setPending} />}
    </div>
  )
}
