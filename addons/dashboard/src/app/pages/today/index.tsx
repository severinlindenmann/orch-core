import { useMemo } from 'react'
import { useQueries, useQuery } from '@tanstack/react-query'
import { CircleCheck, Eye } from 'lucide-react'
import { api } from '@/api/client'
import { can } from '@/api/permissions'
import type { NeedsYouItem, TicketDocument } from '@/api/types'
import { AddonSlotStack } from '@/addon-ui'
import { Skeleton } from '@/components/ui/skeleton'
import { useWorkspace } from '../../workspace'
import { useRole } from '../../useRole'
import { useAttention } from '../../attention'
import { usePageHeader } from '../../shell/ShellUi'
import { AddonDecisionCard, ApprovalCard, QuestionCard, VerdictCard } from './cards'
import { AgentsAtWork, Recently } from './side'
import { displayName, ResolveProvider, type Directory } from './shared'

const itemId = (i: NeedsYouItem) => (i.kind === 'verdict' ? `verdict:${i.ticket}` : `${i.kind === 'approval' ? 'approval' : 'question'}:${i.ticket}:${i.ref}`)

function dateLine(now: string) {
  return new Date(now).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' })
}

export function TodayPage() {
  usePageHeader('Today')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const today = useQuery({ queryKey: ['today', ws], queryFn: () => api.getToday(ws!), enabled: !!ws })
  const agentsQ = useQuery({ queryKey: ['agents', ws], queryFn: () => api.getAgents(ws!), enabled: !!ws })
  const me = useQuery({ queryKey: ['me'], queryFn: api.getMe })
  const decisionsQ = useQuery({ queryKey: ['addon-decisions', ws], queryFn: () => api.getAddonDecisions(ws!), enabled: !!ws })

  const attention = useAttention(ws)
  const role = useRole()
  const readOnly = !can(role, 'ticket.act')

  // Viewers get no personal queue from the API; it sends what is open in the workspace instead, read only.
  const items: NeedsYouItem[] = useMemo(
    () => (today.data ? (readOnly ? today.data.read_only_open : today.data.needs_you) : []),
    [today.data, readOnly],
  )

  const claimTickets = (agentsQ.data ?? []).flatMap((a) => a.claims.map((c) => c.ticket))
  const keys = [...new Set([...items.map((i) => i.ticket), ...claimTickets])]
  const ticketQs = useQueries({ queries: keys.map((k) => ({ queryKey: ['ticket', k], queryFn: () => api.getTicket(k) })) })
  const byKey: Record<string, TicketDocument | undefined> = {}
  keys.forEach((k, i) => (byKey[k] = ticketQs[i]?.data))

  const dir: Directory = { workspace, agents: agentsQ.data ?? [] }

  const ordered = [...items].sort((a, b) => Number(!!b.blocking) - Number(!!a.blocking))
  const agentsWorking = (agentsQ.data ?? []).filter((a) => a.claims.length > 0).length
  const sessions = attention.agents.sessions

  if (!today.data || !agentsQ.data || !me.data) {
    return (
      <div className="space-y-4" aria-busy="true">
        <h1 className="text-xl font-semibold tracking-tight">Today</h1>
        <Skeleton className="h-5 w-96" />
        <Skeleton className="h-40 w-full max-w-3xl" />
      </div>
    )
  }

  const decisions = decisionsQ.data ?? []
  const now = today.data.now

  return (
    <ResolveProvider>
      {({ hidden, notes }) => {
        const visible = ordered.filter((i) => !hidden.has(itemId(i)))
        const visibleDecisions = decisions.filter((d) => !hidden.has(`addon:${d.id}`))
        const count = visible.length + visibleDecisions.length
        return (
          <div className="space-y-6">
            <div className="space-y-1">
              <h1 className="text-xl font-semibold tracking-tight">Today</h1>
              <p className="text-[13px] tabular-nums text-text-muted">
                {dateLine(now)} · {count} decision{count === 1 ? '' : 's'} {readOnly ? 'open' : 'need you'} · {sessions} agent session{sessions === 1 ? '' : 's'}
              </p>
            </div>

            <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
              <section aria-labelledby="needs-h" className="min-w-0 space-y-3">
                <div className="flex items-center gap-2">
                  <h2 id="needs-h" className="text-[13px] font-semibold text-text">
                    {readOnly ? 'Open in the workspace' : 'Needs you'}
                  </h2>
                  {readOnly && (
                    <span className="inline-flex items-center gap-1 rounded bg-surface-2 px-1.5 py-0.5 text-[11px] text-text-muted">
                      <Eye className="size-3" />
                      viewer · read only
                    </span>
                  )}
                </div>

                {notes.map((n) => (
                  <div key={n.id + n.text} className="flex items-start gap-2 rounded-lg border border-border bg-surface px-4 py-2.5 text-[13px]">
                    <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" />
                    <div>
                      <p className="text-text">{n.text}</p>
                      <p className="text-xs text-text-muted">{n.detail}</p>
                    </div>
                  </div>
                ))}

                {visible.map((i) => {
                  const common = { item: i, ticket: byKey[i.ticket], dir, now, readOnly }
                  const k = itemId(i)
                  if (i.kind === 'question') return <QuestionCard key={k} {...common} />
                  if (i.kind === 'approval') return <ApprovalCard key={k} {...common} />
                  if (i.kind === 'verdict') return <VerdictCard key={k} {...common} />
                  return null
                })}
                {visibleDecisions.map((d) => (
                  <AddonDecisionCard key={d.id} d={d} readOnly={readOnly} />
                ))}

                {!readOnly && attention.waitingOnOthers.count > 0 && (
                  <p className="text-xs text-text-muted">
                    {attention.waitingOnOthers.count} more {attention.waitingOnOthers.count === 1 ? 'is' : 'are'} waiting on{' '}
                    {attention.waitingOnOthers.who.map((p) => displayName(dir, p)).join(', ') || 'other people'}.
                  </p>
                )}

                {count === 0 && (
                  <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center">
                    <p className="text-[15px] font-medium text-text">Nothing needs you right now</p>
                    <p className="mt-1 text-[13px] text-text-muted">
                      {agentsWorking > 0
                        ? `${agentsWorking} agent${agentsWorking === 1 ? ' is' : 's are'} working on ${today.data.working.length} ticket${today.data.working.length === 1 ? '' : 's'}. You will see a card here when one needs a decision.`
                        : 'No agent is working either. Pick a ticket from the board to start something.'}
                    </p>
                  </div>
                )}
              </section>

              <aside className="min-w-0 space-y-4">
                <AgentsAtWork agents={agentsQ.data} tickets={byKey} dir={dir} now={now} />
                <AddonSlotStack name="today.card" readOnly={!can(role, 'addon.action')} />
                <Recently recent={today.data.recent} now={now} />
              </aside>
            </div>
          </div>
        )
      }}
    </ResolveProvider>
  )
}
