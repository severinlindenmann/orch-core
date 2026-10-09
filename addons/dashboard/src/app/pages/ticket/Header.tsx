import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Bot, Check, ChevronDown, Copy, Loader2, Lock, MessageSquareReply, Tag, Timer } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import { addonActive } from '@/api/addons'
import { AddonBadge, useAddons, useRunAddonAction, type RunAddonAction } from '@/addon-ui'
import { canSpawnAgent } from '@/addon-ui/capabilities'
import { precheckReason } from '@/addon-ui/SpawnConfirm'
import { useWorkspace } from '@/app/workspace'
import { workspaceOfTicket } from '@/api/workspaces'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { VIEWER_REASON } from '@/components/DisabledReason'
import { BlockedByConnection } from './Needs'
import { availableActions, blockedApproval, GATE_LABEL, primaryAction, primaryLabel } from './actions'
import { agentName, fmtClock, Mono, Pill, StatusChip, type Jump, type HumanAction, type Viewer } from './shared'

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

/** Who is working on the ticket, in one line. Claiming is the agents' job, so people get no Claim/Release here. */
function AgentStatus({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const c = ticket.claim
  const subagents = new Set(ticket.tasks_state.flatMap((t) => (t.lease && t.lease.session !== c?.session ? [t.lease.session] : []))).size
  return (
    <p className="flex min-w-0 items-center gap-2 text-[13px] text-text-muted" data-testid="agent-status">
      <Bot className={c ? 'size-4 shrink-0 text-brand' : 'size-4 shrink-0 text-text-faint'} aria-hidden />
      <span className="min-w-0 truncate">
        {c ? (
          <>
            <span className="text-text">{agentName(c.agent)}</span> is working for <span className="text-text">{viewer.name(c.for)}</span>
            {' · '}since {fmtClock(c.since)}
            {subagents > 0 && ` · ${subagents} ${subagents === 1 ? 'subagent' : 'subagents'}`}
          </>
        ) : (
          'No agent is working on this ticket'
        )}
      </span>
    </p>
  )
}

const START_AGENT = 'start-agent'

/** "Start agent…" in the Actions menu (the start-agent addon, when active here). Core checks the claim before any dialog. */
function StartAgentItem({ ticket, viewer, r }: { ticket: TicketDocument; viewer: Viewer; r: RunAddonAction }) {
  const { workspace } = useWorkspace()
  const { data: addons } = useAddons()
  const pkg = addons?.find((a) => a.name === START_AGENT)
  if (!addonActive(workspace, START_AGENT) || !canSpawnAgent(pkg, workspace?.addons[START_AGENT]) || ticket.status === 'done') return null
  const claimed = precheckReason(ticket, viewer.name)
  const roleBlocked = !r.allowed(START_AGENT, 'start')
  const reason = roleBlocked ? VIEWER_REASON : claimed
  return (
    <>
      <DropdownMenuSeparator />
      <DropdownMenuItem disabled={!!reason} onSelect={() => r.run(START_AGENT, 'start')} className="items-start">
        <Bot className="mt-0.5" />
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            Start agent…
            <AddonBadge name={START_AGENT} title={pkg?.title} className="ml-auto" />
          </span>
          {reason && <span className="mt-0.5 block text-[12px] text-text-muted">{reason}</span>}
        </span>
      </DropdownMenuItem>
    </>
  )
}

export function ActionsMenu({ ticket, viewer, sign, jump, signing = false }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void; signing?: boolean }) {
  const av = availableActions(ticket, viewer)
  const primary = primaryAction(av)
  // The primary action is the header's button; the menu holds the rest.
  const answer = av.answer.filter((q) => !(primary?.kind === 'answer' && primary.question === q.id))
  const approve = av.approve.filter((g) => !(primary?.kind === 'approve' && primary.gate === g))
  const verdict = av.verdict && primary?.kind !== 'verdict'
  const empty = !approve.length && !av.requestChanges.length && !verdict && !answer.length
  // Core's start dialog lives outside the menu, which closes when an item is chosen.
  const r = useRunAddonAction(ticket.key)
  return (
    <>
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" variant="outline" disabled={signing}>
          Actions
          <ChevronDown />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        <DropdownMenuLabel className="text-[11px] font-normal text-text-faint">Human actions, signed with Touch ID</DropdownMenuLabel>
        {empty && <p className="px-2 py-2 text-[13px] text-text-muted">{primary ? 'Nothing else needs you on this ticket.' : 'Nothing needs you on this ticket.'}</p>}
        {answer.map((q) => (
          <DropdownMenuItem key={q.id} onSelect={() => jump({ tab: 'questions', id: q.id })}>
            <MessageSquareReply />
            Answer {q.id}
          </DropdownMenuItem>
        ))}
        {approve.map((g) => (
          <DropdownMenuItem key={g} onSelect={() => sign({ kind: 'approve', gate: g })}>
            <Check />
            Approve {GATE_LABEL[g].toLowerCase()}
          </DropdownMenuItem>
        ))}
        {verdict && (
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
        <StartAgentItem ticket={ticket} viewer={viewer} r={r} />
      </DropdownMenuContent>
    </DropdownMenu>
    {r.dialog}
    </>
  )
}

/** The header's one primary button: what the viewer should do next (hidden when nothing needs them). */
function PrimaryButton({ ticket, viewer, sign, jump, signing }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void; signing: boolean }) {
  const p = primaryAction(availableActions(ticket, viewer))
  if (signing)
    return (
      <Button size="sm" disabled aria-busy>
        <Loader2 className="animate-spin" />
        Signing…
      </Button>
    )
  const blocked = blockedApproval(ticket, viewer)
  // Someone who cannot sign the next gate sees the button, off, with the reason (not an empty header).
  if (!p && blocked)
    return (
      <span title={blocked.reason} className="inline-flex">
        <Button size="sm" disabled aria-describedby={`why-${ticket.key}`}>
          <Check />
          {blocked.label}
        </Button>
        <span id={`why-${ticket.key}`} className="sr-only">
          {blocked.reason}
        </span>
      </span>
    )
  if (!p) return null
  const run = () => {
    if (p.kind === 'answer') jump({ tab: 'questions', id: p.question })
    else if (p.kind === 'verdict') sign({ kind: 'verdict' })
    else sign({ kind: 'approve', gate: p.gate })
  }
  return (
    <Button size="sm" onClick={run}>
      {p.kind === 'answer' ? <MessageSquareReply /> : <Check />}
      {primaryLabel(p)}
    </Button>
  )
}

const MAX_LABELS = 3

export function TicketHeader({ ticket, viewer, sign, jump, signing = false }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void; signing?: boolean }) {
  const children = useQuery({
    queryKey: ['ticket-children', ticket.key],
    queryFn: async () => {
      const ws = workspaceOfTicket(ticket.key, await api.getWorkspaces())
      return ws ? api.listTickets(ws.id, { parent: ticket.key }) : []
    },
    enabled: ticket.type === 'epic',
  })
  const restricted = ticket.visibility !== 'workspace'
  const shown = ticket.labels.slice(0, MAX_LABELS)
  const more = ticket.labels.length - shown.length
  return (
    <header className="min-w-0 space-y-2.5" data-testid="ticket-header">
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
        {restricted && (
          <Pill tone="warning">
            <Lock />
            Restricted
          </Pill>
        )}
      </div>
      <div className="flex min-w-0 items-start gap-3">
        <h1 className="min-w-0 flex-1 text-xl font-semibold leading-tight tracking-tight text-text">{ticket.title}</h1>
        <div className="flex shrink-0 items-center gap-2">
          <PrimaryButton ticket={ticket} viewer={viewer} sign={sign} jump={jump} signing={signing} />
          <ActionsMenu ticket={ticket} viewer={viewer} sign={sign} jump={jump} signing={signing} />
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <StatusChip status={ticket.status} />
        <span className="text-[13px] text-text-muted">
          Turn: <span className="text-text">{viewer.name(ticket.turn.who)}</span> · {ticket.turn.why.charAt(0).toLowerCase() + ticket.turn.why.slice(1)}
        </span>
        {shown.map((l) => (
          <Pill key={l}>
            <Tag />
            <span data-testid="ticket-label">{l}</span>
          </Pill>
        ))}
        {more > 0 && <Pill>+{more}</Pill>}
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

      <BlockedByConnection ticket={ticket} />
      <AgentStatus ticket={ticket} viewer={viewer} />
    </header>
  )
}
