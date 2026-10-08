import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Bot, Check, ChevronDown, Copy, Lock, MessageSquareReply, Tag, Timer } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { availableActions, GATE_LABEL } from './actions'
import { agentName, ago, fmtTime, Mono, PersonChip, Pill, PriorityLabel, StatusChip, type Jump, type HumanAction, type Viewer } from './shared'

export function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard?.writeText(text)
    } catch {
      /* clipboard blocked in the viewer sandbox: still show feedback */
    }
    setDone(true)
    setTimeout(() => setDone(false), 1500)
  }
  return (
    <Button type="button" variant="ghost" size="icon-xs" onClick={copy} aria-label={done ? 'Copied' : label}>
      {done ? <Check className="text-success" /> : <Copy />}
    </Button>
  )
}

function PeopleRow({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const { owner, assignees, reviewers, watchers } = ticket.people
  const group = (label: string, ids: string[], role?: string) =>
    ids.length > 0 && (
      <div className="flex items-center gap-2" aria-label={label}>
        <span className="text-[11px] uppercase tracking-wide text-text-faint">{label}</span>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          {ids.map((id) => (
            <PersonChip key={id} id={id} viewer={viewer} role={role} />
          ))}
        </div>
      </div>
    )
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2" data-testid="people-row">
      {group('Owner', owner ? [owner] : [])}
      {group('Assignees', assignees)}
      {group('Reviewers', reviewers)}
      {group('Watchers', watchers)}
    </div>
  )
}

function ClaimBox({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const c = ticket.claim
  const leases = ticket.tasks_state.filter((t) => t.lease)
  const agentOnly = (verb: string) => `Only agents ${verb} tickets: run "orch ${verb} ${ticket.key}". People cannot ${verb} for an agent.`
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-border bg-surface px-3 py-2 text-[13px]" data-testid="claim-box">
      <Bot className="size-4 shrink-0 text-text-muted" />
      {c ? (
        <p className="min-w-0 flex-1 text-text-muted">
          <span className="font-medium text-text">{agentName(c.agent)}</span> working for <span className="font-medium text-text">{viewer.name(c.for)}</span>
          {' · '}since {fmtTime(c.since)}
          {' · '}expires {fmtTime(c.expires)} <span className="text-text-faint">({ago(c.expires)})</span>
          {leases.length > 0 && (
            <>
              {' · '}leases{' '}
              {leases.map((t, i) => (
                <span key={t.id}>
                  {i > 0 && ', '}
                  <Mono className="text-text">{t.id}</Mono> <span className="text-text-faint">({t.lease!.session.split('.').slice(1).map((n) => `sub${n}`).join('') || t.lease!.session})</span>
                </span>
              ))}
            </>
          )}
        </p>
      ) : (
        <p className="min-w-0 flex-1 text-text-muted">No agent has claimed this ticket.</p>
      )}
      <TooltipProvider delayDuration={150}>
        <div className="flex gap-1.5">
          {[
            { label: 'Claim', verb: 'claim' },
            { label: 'Release', verb: 'release' },
          ].map(({ label, verb }) => (
            <Tooltip key={label}>
              <TooltipTrigger asChild>
                <span tabIndex={0} className="inline-flex rounded-md focus-visible:ring-[3px] focus-visible:ring-ring/50">
                  <Button size="sm" variant="outline" disabled aria-label={`${label} (agents only)`}>
                    {label}
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent className="max-w-xs">{agentOnly(verb)}</TooltipContent>
            </Tooltip>
          ))}
        </div>
      </TooltipProvider>
    </div>
  )
}

export function ActionsMenu({ ticket, viewer, sign, jump }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void }) {
  const av = availableActions(ticket, viewer)
  const empty = !av.approve.length && !av.requestChanges.length && !av.verdict && !av.answer.length
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" variant="outline">
          Actions
          <ChevronDown />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="text-[11px] font-normal text-text-faint">Human actions, signed with Touch ID</DropdownMenuLabel>
        {empty && <p className="px-2 py-2 text-[13px] text-text-muted">Nothing needs you on this ticket.</p>}
        {av.answer.map((q) => (
          <DropdownMenuItem key={q.id} onSelect={() => jump({ tab: 'questions', id: q.id })}>
            <MessageSquareReply />
            Answer {q.id}
          </DropdownMenuItem>
        ))}
        {av.approve.map((g) => (
          <DropdownMenuItem key={g} onSelect={() => sign({ kind: 'approve', gate: g })}>
            <Check />
            Approve {GATE_LABEL[g].toLowerCase()}
          </DropdownMenuItem>
        ))}
        {av.verdict && (
          <DropdownMenuItem onSelect={() => sign({ kind: 'verdict' })}>
            <Check />
            Give verdict
          </DropdownMenuItem>
        )}
        {av.requestChanges.length > 0 && <DropdownMenuSeparator />}
        {av.requestChanges.map((g) => (
          <DropdownMenuItem key={g} onSelect={() => sign({ kind: 'request_changes', gate: g })}>
            <Timer />
            Request changes on {GATE_LABEL[g].toLowerCase()}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function TicketHeader({ ticket, viewer, sign, jump }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void }) {
  const children = useQuery({
    queryKey: ['ticket-children', ticket.key],
    queryFn: async () => {
      const ws = (await api.getWorkspaces()).find((w) => w.prefix === ticket.key.split('-')[0])
      return ws ? api.listTickets(ws.id, { parent: ticket.key }) : []
    },
    enabled: ticket.type === 'epic',
  })
  const restricted = ticket.visibility !== 'workspace'
  return (
    <header className="space-y-3">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <Mono className="text-text-muted">{ticket.key}</Mono>
            <CopyButton text={ticket.key} label={`Copy ${ticket.key}`} />
            <Pill className="capitalize">{ticket.type}</Pill>
            {ticket.parent && (
              <span className="text-text-muted">
                in{' '}
                <Link to="/ticket/$key" params={{ key: ticket.parent }} className="font-mono text-[12px] text-brand hover:underline">
                  {ticket.parent}
                </Link>
              </span>
            )}
          </div>
          <h1 className="text-xl font-semibold leading-tight tracking-tight text-text">{ticket.title}</h1>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <StatusChip status={ticket.status} />
            <PriorityLabel priority={ticket.priority} />
            {ticket.size && <Pill className="uppercase">{ticket.size}</Pill>}
            {restricted && (
              <Pill tone="warning">
                <Lock />
                Restricted
              </Pill>
            )}
            {ticket.labels.map((l) => (
              <Pill key={l}>
                <Tag />
                {l}
              </Pill>
            ))}
            <span className="text-[12px] text-text-muted">
              Turn: <span className="text-text">{viewer.name(ticket.turn.who)}</span> · {ticket.turn.why}
            </span>
          </div>
        </div>
        <ActionsMenu ticket={ticket} viewer={viewer} sign={sign} jump={jump} />
      </div>

      {restricted && typeof ticket.visibility === 'object' && (
        <p className="flex items-center gap-2 text-[12px] text-text-muted">
          <Lock className="size-3.5 text-warning" />
          Visible only to {ticket.visibility.restricted.map((p) => viewer.name(p)).join(' and ')}. Other members get &quot;not visible&quot;.
        </p>
      )}

      {ticket.type === 'epic' && (
        <div className="rounded-lg border border-border bg-surface px-3 py-2">
          <p className="mb-1.5 text-[11px] uppercase tracking-wide text-text-faint">Children ({children.data?.length ?? ticket.children?.length ?? 0})</p>
          <ul className="grid gap-1 sm:grid-cols-2">
            {(children.data ?? (ticket.children ?? []).map((k) => ({ key: k, title: '', status: ticket.status, progress: null }))).map((c) => (
              <li key={c.key}>
                <Link to="/ticket/$key" params={{ key: c.key }} className="flex items-center gap-2 rounded-md px-1.5 py-1 text-[13px] hover:bg-surface-2">
                  <Mono className="text-text-muted">{c.key}</Mono>
                  <span className="min-w-0 flex-1 truncate">{c.title}</span>
                  {'progress' in c && c.progress && (
                    <span className="font-mono text-[11px] text-text-faint">
                      {c.progress.tasks_done}/{c.progress.tasks_total}
                    </span>
                  )}
                  {c.title && <StatusChip status={c.status} />}
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}

      <PeopleRow ticket={ticket} viewer={viewer} />
      <ClaimBox ticket={ticket} viewer={viewer} />
    </header>
  )
}
