import { can } from '@/api/permissions'
import { Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Bot, Check, ChevronDown, CircleHelp, Copy, Loader2, Lock, Tag, Timer } from 'lucide-react'
import { useState } from 'react'
import { api } from '@/api/client'
import { addonActive } from '@/api/addons'
import { AddonBadge, useAddons, useRunAddonAction } from '@/addon-ui'
import { canSpawnAgent } from '@/addon-ui/capabilities'
import { hasFeature, isRealHost } from '@/api/features'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { precheckReason } from '@/addon-ui/SpawnConfirm'
import { useWorkspace } from '@/app/workspace'
import { workspaceOfTicket } from '@/api/workspaces'
import type { ClosedInfo, TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { VIEWER_REASON } from '@/components/DisabledReason'
import { CopyLinkButton } from '@/app/shell/CopyLinkButton'
import { BlockedByConnection } from './Needs'
import { availableActions, gateActionName, blockedApproval, primaryAction, primaryLabel } from './actions'
import { agentName, ago, fmtClock, KeyRef, Mono, Pill, StatusChip, useIsVisibleKey, type HumanAction, type Jump, type Viewer } from './shared'

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
export function AgentStatus({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  // A claim the host marks not live (ended by Done or Close, lapsed, grant over) is history: nobody is working (run 8, 8).
  const c = ticket.claim?.live === false ? null : ticket.claim
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

/** The person-visible reason no agent can start now (an agent works on it already), or null. A lapsed claim is history. */
export const WORKING_REASON = 'An agent is already working on this ticket'
const agentWorks = (t: TicketDocument) => !!t.claim && t.claim.live !== false

export interface StartAgent {
  /** "Start agent" is part of this ticket's actions (the addon or core's agent.start is available here). */
  shown: boolean
  /** Why it is off right now (shown inline), null when it can start. */
  reason: string | null
  /** The primary button may offer it: nothing blocks it and the ticket is in a state an agent works on. */
  startable: boolean
  run: () => void
  /** The start-agent addon's own title, for its badge in the mock (none on a real host). */
  addon?: { name: string; title?: string }
  node: React.ReactNode
}

/**
 * "Start agent" for this ticket, on a real host through core's dialog (hasFeature agent.start), in the mock through the
 * start-agent addon. The header's primary button and the Actions menu share it, so there is one dialog and one rule.
 */
function useStartAgent(ticket: TicketDocument, viewer: Viewer): StartAgent {
  const { workspace } = useWorkspace()
  const { data: addons } = useAddons()
  const r = useRunAddonAction(ticket.key)
  const [open, setOpen] = useState(false)
  const real = isRealHost()
  const pkg = addons?.find((a) => a.name === START_AGENT)
  const finished = ticket.status === 'done' || ticket.status === 'closed'
  const shown = real
    ? !!workspace && hasFeature('agent_start', workspace.id) && !finished
    : !!workspace && addonActive(workspace, START_AGENT) && canSpawnAgent(pkg, workspace.addons[START_AGENT]) && !finished
  const roleBlocked = real ? !can(viewer.role, 'ticket.act') : !r.allowed(START_AGENT, 'start')
  const reason = roleBlocked ? VIEWER_REASON : agentWorks(ticket) ? WORKING_REASON : real ? null : precheckReason(ticket, viewer.name)
  const run = () => (real ? setOpen(true) : r.run(START_AGENT, 'start'))
  const startable = shown && !reason && ticket.status !== 'testing'
  const node = (
    <>
      {r.dialog}
      {open && workspace && <StartAgentDialog ws={workspace.id} ticket={ticket.key} open onClose={() => setOpen(false)} />}
    </>
  )
  return { shown, reason, startable, run, node, addon: real ? undefined : { name: START_AGENT, title: pkg?.title } }
}

/** " · why it is their turn", or nothing when the host gave no reason (no dangling separator). */
export function turnReason(why: string): string {
  const text = why.trim()
  return text ? ` · ${text.charAt(0).toLowerCase()}${text.slice(1)}` : ''
}

function Next() {
  return (
    <span className="ml-auto rounded-full bg-brand/15 px-1.5 py-px text-[10px] font-medium text-brand" data-testid="next-tag">
      next
    </span>
  )
}

/** The header's Actions menu lists EVERY action the viewer has, the primary one included (tagged "next"). */
export function ActionsMenu({ ticket, viewer, sign, jump, start, signing = false }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void; start: StartAgent; signing?: boolean }) {
  const av = availableActions(ticket, viewer)
  const primary = primaryAction(av, { start: start.startable })
  const isNext = (k: string) => (primary?.kind === 'approve' ? `approve:${primary.gate}` : primary?.kind === 'answer' ? `answer:${primary.question}` : primary?.kind) === k
  const needs = av.approve.length > 0 || av.verdict || av.answer.length > 0
  const other = av.requestChanges.length > 0 || start.shown
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" variant="outline" disabled={signing}>
          Actions
          <ChevronDown />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        {!needs && !other && <p className="px-2 py-2 text-[13px] text-text-muted">Nothing needs you on this ticket.</p>}
        {needs && <DropdownMenuLabel className="text-[11px] font-normal text-text-faint">Needs you</DropdownMenuLabel>}
        {av.answer.map((q) => (
          <DropdownMenuItem key={q.id} onSelect={() => jump({ tab: 'questions', id: `question-${q.id}` })}>
            <CircleHelp />
            Answer question{av.answer.length > 1 ? ` ${q.id}` : ''}
            {isNext(`answer:${q.id}`) && <Next />}
          </DropdownMenuItem>
        ))}
        {av.verdict && (
          <DropdownMenuItem onSelect={() => sign({ kind: 'verdict' })}>
            <Check />
            Give verdict
            {isNext('verdict') && <Next />}
          </DropdownMenuItem>
        )}
        {av.approve.map((g) => (
          <DropdownMenuItem key={g} onSelect={() => sign({ kind: 'approve', gate: g })}>
            <Check />
            Approve {gateActionName(g)}
            {isNext(`approve:${g}`) && <Next />}
          </DropdownMenuItem>
        ))}
        {needs && other && <DropdownMenuSeparator />}
        {other && <DropdownMenuLabel className="text-[11px] font-normal text-text-faint">Other</DropdownMenuLabel>}
        {av.requestChanges.map((g) => (
          <DropdownMenuItem key={g} onSelect={() => sign({ kind: 'request_changes', gate: g })}>
            <Timer />
            {g === 'verify' ? 'Send back' : `Request changes on ${gateActionName(g)}`}
          </DropdownMenuItem>
        ))}
        {start.shown && (
          <DropdownMenuItem disabled={!!start.reason} onSelect={start.run} className="items-start" data-testid="real-start-agent">
            <Bot className="mt-0.5" />
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2">
                Start agent
                {start.addon && <AddonBadge name={start.addon.name} title={start.addon.title} className="ml-auto" />}
                {isNext('start') && <Next />}
              </span>
              {start.reason && <span className="mt-0.5 block text-[12px] text-text-muted">{start.reason}</span>}
            </span>
          </DropdownMenuItem>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** The header's one primary button: what the viewer should do next (a quiet status when an agent works and nothing waits). */
function PrimaryButton({ ticket, viewer, sign, jump, start, signing }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void; start: StartAgent; signing: boolean }) {
  const av = availableActions(ticket, viewer)
  const blocked = blockedApproval(ticket, viewer)
  // A gate that waits for someone else (shown disabled below) comes before offering to start an agent.
  const p = primaryAction(av, { start: start.startable && !blocked })
  if (signing)
    return (
      <Button size="sm" disabled aria-busy>
        <Loader2 className="animate-spin" />
        Signing…
      </Button>
    )
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
  if (!p) return agentWorks(ticket) && ticket.status !== 'done' ? <span className="inline-flex items-center gap-1.5 text-[13px] text-text-muted" data-testid="agent-working-note"><Loader2 className="size-3.5 animate-spin" aria-hidden />Agent is working…</span> : null
  const run = () => {
    if (p.kind === 'verdict') sign({ kind: 'verdict' })
    else if (p.kind === 'answer') jump({ tab: 'questions', id: `question-${p.question}` })
    else if (p.kind === 'start') start.run()
    else sign({ kind: 'approve', gate: p.gate })
  }
  return (
    <Button size="sm" onClick={run}>
      {p.kind === 'answer' ? <CircleHelp /> : p.kind === 'start' ? <Bot /> : <Check />}
      {primaryLabel(p, av)}
    </Button>
  )
}

const MAX_LABELS = 3

const RESOLUTION: Record<ClosedInfo['resolution'], string> = {
  wont_do: "Won't do",
  duplicate: 'Duplicate',
  obsolete: 'Obsolete',
  other: 'Closed',
}

/**
 * A closed ticket reads like any other (host v1.4): this says why and when. `duplicate_of` may name a ticket you cannot
 * open (it would answer "not found"), so it is plain text, never a link and never a title.
 */
function ClosedNotice({ closed, name }: { closed: ClosedInfo; name: (id: string) => string }) {
  return (
    <section aria-label="Closed" data-testid="closed-notice" className="space-y-1 rounded-md border border-border bg-surface-2 px-3 py-2 text-[13px] text-text-muted">
      <p>
        <span className="font-medium text-text">Closed: {RESOLUTION[closed.resolution] ?? closed.resolution}</span>
        {closed.duplicate_of && (
          <>
            {' · of '}
            <span className="font-mono text-text">{closed.duplicate_of}</span>
          </>
        )}
        {closed.by && ` · by ${name(closed.by)}`}
        {closed.at && ` · ${ago(closed.at)}`}
      </p>
      {closed.text && <p className="whitespace-pre-wrap text-text">{closed.text}</p>}
    </section>
  )
}

export function TicketHeader({ ticket, viewer, sign, jump, signing = false }: { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void; signing?: boolean }) {
  const { workspaces } = useWorkspace()
  // The ticket's own workspace: the rows come from it, so they are keyed by it too and its gone / revoked / ready
  // purge reaches them (the live cache rules only see keys with the id).
  const home = workspaceOfTicket(ticket.key, workspaces)?.id
  const isVisible = useIsVisibleKey()
  const children = useQuery({
    queryKey: ['ticket-children', home, ticket.key],
    queryFn: () => (home ? api.listTickets(home, { parent: ticket.key }) : []),
    enabled: ticket.type === 'epic' && !!home,
  })
  const start = useStartAgent(ticket, viewer)
  const restricted = ticket.visibility !== 'workspace'
  const shown = ticket.labels.slice(0, MAX_LABELS)
  const more = ticket.labels.length - shown.length
  return (
    <header className="min-w-0 space-y-2.5" data-testid="ticket-header">
      <div className="flex flex-wrap items-center gap-2 text-[13px]">
        <Mono className="text-text-muted">{ticket.key}</Mono>
        <CopyButton text={ticket.key} label={`Copy ${ticket.key}`} />
        <CopyLinkButton label={`Copy link to ${ticket.key}`} what={`Link to ${ticket.key}`} />
        <Pill className="capitalize">{ticket.type}</Pill>
        {ticket.parent && (
          <span className="text-text-muted">
            in{' '}
            <KeyRef k={ticket.parent} visible={isVisible(ticket.parent)} />
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
          <PrimaryButton ticket={ticket} viewer={viewer} sign={sign} jump={jump} start={start} signing={signing} />
          <ActionsMenu ticket={ticket} viewer={viewer} sign={sign} jump={jump} start={start} signing={signing} />
          {start.node}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <StatusChip status={ticket.status} landing={ticket.landing} />
        <span className="text-[13px] text-text-muted">
          Turn: <span className="text-text">{viewer.name(ticket.turn.who)}</span>{turnReason(ticket.turn.why)}
        </span>
        {shown.map((l) => (
          <Pill key={l}>
            <Tag />
            <span data-testid="ticket-label">{l}</span>
          </Pill>
        ))}
        {more > 0 && <Pill>+{more}</Pill>}
      </div>

      {ticket.closed && <ClosedNotice closed={ticket.closed} name={viewer.name} />}

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
