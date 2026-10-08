import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { BadgeCheck, CalendarClock, CircleHelp, ExternalLink, GitBranch, GitPullRequest, Hand, Hourglass, ShieldQuestion } from 'lucide-react'
import { api } from '@/api/client'
import type { NeedsYouItem, TicketDocument } from '@/api/types'
import { addonActive } from '@/api/addons'
import { AddonSlotStack, AddonBadge, useAddons } from '@/addon-ui'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { availableActions } from './actions'
import { ago, fmtTime, Mono, Section, shortHash, type Jump, type HumanAction, type Viewer } from './shared'

const NEED_ICON: Record<NeedsYouItem['kind'], typeof CircleHelp> = {
  question: CircleHelp,
  verdict: BadgeCheck,
  approval: ShieldQuestion,
  handoff: Hand,
  expiring: Hourglass,
}

const isHttp = (u: string) => /^https?:\/\//i.test(u)

function NeedsYou({ ticket, viewer, sign, jump }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void }) {
  const ws = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const wsId = ws.data?.find((w) => w.prefix === ticket.key.split('-')[0])?.id
  const today = useQuery({ queryKey: ['today', wsId], queryFn: () => api.getToday(wsId!), enabled: !!wsId })
  const items = (today.data?.needs_you ?? []).filter((n) => n.ticket === ticket.key)
  const av = availableActions(ticket, viewer)

  const act = (n: NeedsYouItem) => {
    if (n.kind === 'question' && n.ref) return { label: `Answer ${n.ref}`, run: () => jump({ tab: 'questions', id: n.ref }) }
    if (n.kind === 'verdict' && av.verdict) return { label: 'Give verdict', run: () => sign({ kind: 'verdict' }) }
    if (n.kind === 'approval' && (n.ref === 'requirements' || n.ref === 'plan') && av.approve.includes(n.ref))
      return { label: `Approve ${n.ref}`, run: () => sign({ kind: 'approve', gate: n.ref as 'requirements' | 'plan' }) }
    return null
  }

  return (
    <Section title={`Needs you${items.length ? ` (${items.length})` : ''}`}>
      {today.isLoading ? (
        <p className="text-[13px] text-text-faint">Checking</p>
      ) : items.length === 0 ? (
        <p className="text-[13px] text-text-muted">Nothing needs you on this ticket.</p>
      ) : (
        <ul className="space-y-2">
          {items.map((n) => {
            const Icon = NEED_ICON[n.kind]
            const a = act(n)
            return (
              <li key={`${n.kind}-${n.ref}`} className="rounded-md border border-border bg-bg p-2.5">
                <div className="flex items-start gap-2">
                  <Icon className="mt-0.5 size-4 shrink-0 text-warning" />
                  <div className="min-w-0 flex-1">
                    <p className="text-[13px] leading-snug">{n.text}</p>
                    <p className="mt-0.5 text-[11px] text-text-faint">
                      {n.ref && <Mono className="text-[11px]">{n.ref}</Mono>}
                      {n.blocking && ' · blocking'} · {ago(n.since)}
                    </p>
                  </div>
                </div>
                {a && (
                  <Button size="xs" variant="outline" className="mt-2" onClick={a.run}>
                    {a.label}
                  </Button>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}

/** Data an addon wrote on this ticket while it was active; read-only and collapsed until opened. */
function InactiveAddonData({ ticket }: { ticket: TicketDocument }) {
  const { workspace } = useWorkspace()
  const { data: addons = [] } = useAddons()
  const [open, setOpen] = useState<string | null>(null)
  const names = workspace ? Object.keys(ticket.addons ?? {}).filter((n) => !addonActive(workspace, n)) : []
  if (names.length === 0) return null
  return (
    <Section title="Inactive addons">
      <ul className="space-y-1.5">
        {names.map((n) => {
          const title = addons.find((a) => a.name === n)?.title ?? n
          const isOpen = open === n
          return (
            <li key={n} className="rounded-md border border-border bg-bg text-text-muted opacity-70">
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : n)}
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-[13px]"
              >
                <AddonBadge name={n} />
                <span>{title} · inactive</span>
              </button>
              {isOpen && (
                <pre className="max-h-60 overflow-auto border-t border-border px-2.5 py-2 font-mono text-[11px] leading-snug text-text-muted">
                  {JSON.stringify(ticket.addons[n], null, 2)}
                </pre>
              )}
            </li>
          )
        })}
      </ul>
    </Section>
  )
}

export function Rail({ ticket, viewer, sign, jump }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void }) {
  const branches = Object.entries(ticket.links.branches)
  const row = (label: string, value: React.ReactNode) => (
    <div className="flex items-baseline gap-2 py-1 text-[13px]">
      <dt className="w-20 shrink-0 text-text-muted">{label}</dt>
      <dd className="min-w-0 flex-1">{value}</dd>
    </div>
  )
  return (
    <aside className="space-y-3" aria-label="Ticket details">
      <NeedsYou ticket={ticket} viewer={viewer} sign={sign} jump={jump} />

      <Section title="Details">
        <dl className="divide-y divide-border">
          {row('Size', ticket.size ? <span className="uppercase">{ticket.size}</span> : <span className="text-text-faint">not set</span>)}
          {row(
            'Due',
            ticket.due ? (
              <span className="inline-flex items-center gap-1.5">
                <CalendarClock className="size-3.5 text-text-muted" />
                {ticket.due}
              </span>
            ) : (
              <span className="text-text-faint">none</span>
            ),
          )}
          {ticket.blocked_by.length > 0 &&
            row(
              'Blocked by',
              <span className="flex flex-wrap gap-1.5">
                {ticket.blocked_by.map((k) => (
                  <Link key={k} to="/ticket/$key" params={{ key: k }} className="font-mono text-[12px] text-brand hover:underline">
                    {k}
                  </Link>
                ))}
              </span>,
            )}
          {row('Created', <span className="text-text-muted">{fmtTime(ticket.created_at)}</span>)}
          {row('Updated', <span className="text-text-muted">{ago(ticket.updated_at)}</span>)}
          {row(
            'Head',
            <Mono className="text-text-muted" >
              seq {ticket.head.seq} · {shortHash(ticket.head.hash, 8)}
            </Mono>,
          )}
        </dl>
      </Section>

      <Section title="Branches and pull requests">
        {branches.length === 0 && ticket.links.prs.length === 0 && ticket.links.external.length === 0 ? (
          <p className="text-[13px] text-text-faint">Nothing linked yet.</p>
        ) : (
          <ul className="space-y-2 text-[13px]">
            {branches.map(([repo, branch]) => (
              <li key={repo} className="flex items-start gap-2">
                <GitBranch className="mt-0.5 size-3.5 shrink-0 text-text-muted" />
                <span className="min-w-0">
                  <span className="block break-all font-mono text-[12px]">{branch}</span>
                  <span className="text-[11px] text-text-faint">{repo}</span>
                </span>
              </li>
            ))}
            {ticket.links.prs.map((pr) => (
              <li key={pr.url} className="flex items-start gap-2">
                <GitPullRequest className="mt-0.5 size-3.5 shrink-0 text-text-muted" />
                {isHttp(pr.url) ? (
                  <a href={pr.url} target="_blank" rel="noopener noreferrer nofollow" className="min-w-0 break-all text-brand hover:underline">
                    {pr.repo} #{pr.url.split('/').pop()}
                    <ExternalLink className="ml-1 inline size-3" aria-hidden />
                  </a>
                ) : (
                  <span>{pr.repo}</span>
                )}
              </li>
            ))}
            {ticket.links.external.map((l) => (
              <li key={l.url} className="flex items-start gap-2">
                <ExternalLink className="mt-0.5 size-3.5 shrink-0 text-text-muted" />
                {isHttp(l.url) ? (
                  <a href={l.url} target="_blank" rel="noopener noreferrer nofollow" className="text-brand hover:underline">
                    {l.label}
                  </a>
                ) : (
                  <span>{l.label}</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <AddonSlotStack name="ticket.panel" ctx={{ ticket }} />
      <InactiveAddonData ticket={ticket} />
    </aside>
  )
}
