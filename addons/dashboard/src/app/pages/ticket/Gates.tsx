import { CheckCircle2, ChevronRight, CircleDashed, CircleDot, MessageSquareWarning, ShieldAlert } from 'lucide-react'
import type { GateName, GateStatus, TicketDocument } from '@/api/types'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { availableActions, GATE_LABEL, policyText } from './actions'
import { fmtDay, fmtExact, Mono, type Viewer } from './shared'

const GATES: GateName[] = ['requirements', 'plan', 'verify']

const VIA: Record<string, string> = { cli: 'CLI', dashboard: 'dashboard', phone: 'phone', factory_charter: 'the factory charter' }
const PRESENCE: Record<string, string> = { touchid: 'Touch ID', passkey: 'passkey', password: 'password' }

type Tone = 'success' | 'warning' | 'danger' | 'neutral'
const TONE: Record<Tone, string> = { success: 'text-success', warning: 'text-warning', danger: 'text-danger', neutral: 'text-text-muted' }

/** One step's state in words, from the viewer's side: "Approved", "Waiting for your verdict", "Pending". */
function stepState(name: GateName, gate: GateStatus, ticket: TicketDocument, mine: boolean): { text: string; tone: Tone; Icon: typeof CheckCircle2 } {
  if (gate.state === 'approved') return { text: 'Approved', tone: 'success', Icon: CheckCircle2 }
  if (gate.state === 'changes_requested') return { text: 'Changes requested', tone: 'danger', Icon: MessageSquareWarning }
  if (gate.state === 'invalidated') return { text: 'Invalidated', tone: 'warning', Icon: ShieldAlert }
  if (mine) return { text: name === 'verify' ? 'Waiting for your verdict' : 'Waiting for your approval', tone: 'warning', Icon: CircleDot }
  const ready = name === 'verify' ? ticket.status === 'testing' : name === 'plan' ? ticket.gates.requirements.state === 'approved' && ticket.tasks.length > 0 : !!ticket.body.requirements?.trim()
  if (ready) return { text: name === 'verify' ? 'Waiting for a verdict' : 'Waiting for approval', tone: 'neutral', Icon: CircleDot }
  return { text: 'Pending', tone: 'neutral', Icon: CircleDashed }
}

/** Who signed, in plain words; the hash, channel and presence live under Details. */
function Approvals({ gate, viewer }: { gate: GateStatus; viewer: Viewer }) {
  // An invalidation voids the approvals (they no longer count); they stay visible, struck through, until a new one.
  const voided = gate.state === 'invalidated' && gate.approvals.length === 0 ? (gate.voided ?? []) : []
  if (gate.approvals.length === 0 && voided.length === 0) return <p className="text-[12px] text-text-muted">0 of {gate.needed} approvals</p>
  const stale = voided.length > 0
  return (
    <ul className="space-y-1">
      {(stale ? voided : gate.approvals).map((a, i) => (
        <li key={i} className={cn('text-[12px]', stale ? 'text-text-muted line-through decoration-text-faint' : 'text-text')}>
          Approved by {viewer.name(a.by)}, {fmtDay(a.at)}
          {a.via === 'factory_charter' ? ' · auto-approved under the factory charter' : ''}
          {' · '}
          {a.sig_ok === undefined ? (
            <span className="text-text-muted">signature not checked</span>
          ) : (
            <span className={a.sig_ok ? 'text-success' : 'text-danger'}>{a.sig_ok ? 'verified signature' : 'signature failed'}</span>
          )}
        </li>
      ))}
    </ul>
  )
}

function Step({ name, ticket, viewer, mine }: { name: GateName; ticket: TicketDocument; viewer: Viewer; mine: boolean }) {
  const gate = ticket.gates[name]
  const s = stepState(name, gate, ticket, mine)
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid={`gate-${name}`}
          data-state={gate.state}
          aria-label={`${GATE_LABEL[name]}: ${s.text}`}
          className="flex h-8 min-w-0 items-center gap-1.5 rounded-md px-2 text-[13px] hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-ring data-[state=open]:bg-surface-2"
        >
          <span className="shrink-0 font-medium text-text">{GATE_LABEL[name]}</span>
          <s.Icon className={cn('size-3.5 shrink-0', TONE[s.tone])} aria-hidden />
          <span className={cn('truncate', TONE[s.tone])}>{s.text}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" aria-label={`${GATE_LABEL[name]} gate`} className="w-80 space-y-2 border-border bg-surface p-3">
        <p className="text-[13px] font-semibold text-text">
          {GATE_LABEL[name]} · <span className={TONE[s.tone]}>{s.text}</span>
        </p>
        <p className="text-[12px] text-text-muted">{policyText(name, gate)}</p>
        <Approvals gate={gate} viewer={viewer} />
        {(gate.approvals.length > 0 || gate.hash) && (
          <details className="text-[12px] text-text-muted">
            <summary className="cursor-pointer select-none text-text-muted hover:text-text">Details</summary>
            <dl className="mt-1.5 space-y-1">
              {gate.hash && (
                <div>
                  <dt className="inline text-text-faint">Hash </dt>
                  <dd className="inline break-all">
                    <Mono className="text-[11px]">{gate.hash}</Mono>
                  </dd>
                </div>
              )}
              {gate.approvals.map((a, i) => (
                <div key={i}>
                  <dt className="inline text-text-faint">{viewer.name(a.by)} </dt>
                  <dd className="inline">
                    {fmtExact(a.at)} · via {a.via ? VIA[a.via] ?? a.via : 'unknown'}
                    {a.via === 'factory_charter' ? '' : ` · presence ${a.presence ? PRESENCE[a.presence] ?? a.presence : 'unknown'}`}
                  </dd>
                </div>
              ))}
              {gate.covers && gate.covers.length > 0 && (
                <div>
                  <dt className="text-text-faint">Covers</dt>
                  <dd>{gate.covers.join(' · ')}</dd>
                </div>
              )}
            </dl>
          </details>
        )}
      </PopoverContent>
    </Popover>
  )
}

/**
 * The three gates as one stepper line. Each step opens a popover with the policy, who signed and a folded Details
 * (hash, channel, presence). A note or invalidation reason stays visible under the line.
 */
export function GatesStrip({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  const av = availableActions(ticket, viewer)
  const mine = (g: GateName) => (g === 'verify' ? av.verdict : av.approve.includes(g))
  const notes = GATES.flatMap((g): { g: GateName; text: string; tone: 'warning' | 'danger' }[] => {
    const s = ticket.gates[g]
    if (s.state === 'invalidated' && s.reason) return [{ g, text: s.reason, tone: 'warning' }]
    if (s.state === 'changes_requested' && s.note) return [{ g, text: s.note, tone: 'danger' }]
    return []
  })
  return (
    <div className="min-w-0 space-y-1.5">
      <div role="group" aria-label="Gates" data-testid="gates-strip" className="flex min-h-10 min-w-0 flex-wrap items-center gap-x-0.5 rounded-lg border border-border bg-surface px-1">
        {GATES.map((g, i) => (
          <div key={g} className="flex min-w-0 items-center gap-0.5">
            <Step name={g} ticket={ticket} viewer={viewer} mine={mine(g)} />
            {i < GATES.length - 1 && <ChevronRight className="size-3.5 shrink-0 text-text-faint" aria-hidden />}
          </div>
        ))}
      </div>
      {notes.length > 0 && (
        <ul data-testid="gate-notes" className="space-y-1">
          {notes.map((n) => (
            <li
              key={n.g}
              className={cn(
                'flex items-start gap-1.5 rounded-md border px-2 py-1.5 text-[12px]',
                n.tone === 'warning' ? 'border-warning/30 bg-warning-soft text-warning' : 'border-danger/30 bg-danger-soft text-danger',
              )}
            >
              {n.tone === 'warning' ? <ShieldAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden /> : <MessageSquareWarning className="mt-0.5 size-3.5 shrink-0" aria-hidden />}
              <span>
                <span className="font-medium">{GATE_LABEL[n.g]}:</span> {n.text}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
