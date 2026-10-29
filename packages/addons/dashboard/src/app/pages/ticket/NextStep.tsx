import { Check, Info, Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'
import type { GateName, GateStatus, TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { trustedPress } from '@/components/sign/trustedPress'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { openDockOn } from '@/app/terminal/dock/request'
import { useNow } from '@/lib/useNow'
import { activityLine } from './activity'
import { agentWorking, type ProgressRun } from './progress'
import { availableActions, gapOf, GATE_LABEL, policyText } from './actions'
import { fmtDay, fmtExact, type HumanAction, type Jump, Mono, presenceLabel, type Viewer } from './shared'

type Props = {
  ticket: TicketDocument
  viewer: Viewer
  sign: (a: HumanAction) => void
  jump: (j: Jump) => void
  signing?: boolean
  /** The newest agent run of this ticket; without one nothing ran, so nothing "stopped". */
  run?: ProgressRun | null
  /** The run id of the ticket's open terminal wait: the agent asks something in its terminal. */
  wait?: string | null
  /** "Ask the agent to finish" on the not-finished card (absent: the viewer may not start an agent, no button). */
  onFinish?: () => void
  /** "Add it myself": opens the edit sheet (absent: the viewer may not edit, no button). */
  onEdit?: () => void
  openTerminal?: (run: string) => void
}
type StepId = 'understand' | 'build' | 'check' | 'done'
type StepState = 'done' | 'current' | 'todo'

const STEPS: { id: StepId; label: string }[] = [
  { id: 'understand', label: 'Understand' },
  { id: 'build', label: 'Build' },
  { id: 'check', label: 'Check' },
  { id: 'done', label: 'Done' },
]

// Read as a plain string: a closed ticket is finished as well.
const finishedOf = (t: TicketDocument) => ['done', 'closed'].includes(t.status as string)

/** The four plain steps: done ones, the first one not done is current, the rest still to do. */
export function stepStates(ticket: TicketDocument): Record<StepId, StepState> {
  const finished = finishedOf(ticket)
  const built = finished || (ticket.status as string) === 'testing'
  const g = ticket.gates
  const done: Record<StepId, boolean> = {
    understand: built || (g.requirements.state === 'approved' && g.plan.state === 'approved'),
    build: built,
    check: finished || (g.verify.state === 'approved' && (!g.code.required || g.code.state === 'approved')),
    done: finished,
  }
  const current = STEPS.find((s) => !done[s.id])?.id
  return Object.fromEntries(STEPS.map((s) => [s.id, done[s.id] ? 'done' : s.id === current ? 'current' : 'todo'])) as Record<StepId, StepState>
}

function Stepper({ ticket }: { ticket: TicketDocument }) {
  const states = stepStates(ticket)
  return (
    <ol data-testid="stepper" aria-label="Progress" className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 px-1 text-[13px]">
      {STEPS.map((s, i) => {
        const state = states[s.id]
        return (
          <li key={s.id} data-testid={`step-${s.id}`} data-state={state} aria-current={state === 'current' ? 'step' : undefined} className="flex items-center gap-1.5">
            <span
              aria-hidden
              className={cn(
                'flex size-4 shrink-0 items-center justify-center rounded-full border',
                state === 'done' ? 'border-success bg-success-soft text-success' : state === 'current' ? 'border-brand' : 'border-border',
              )}
            >
              {state === 'done' && <Check className="size-2.5" />}
              {state === 'current' && <span className="size-1.5 rounded-full bg-brand" />}
            </span>
            <span className={state === 'todo' ? 'text-text-muted' : 'text-text'}>{s.label}</span>
            {i < STEPS.length - 1 && <span aria-hidden className="ml-0.5 h-px w-4 bg-border" />}
          </li>
        )
      })}
    </ol>
  )
}

// Phase 1 phone decisions have no authentication label (the reader declaration covers answers only).
const phoneDecision = (a: GateStatus['approvals'][number]) => a.via === 'phone' || a.presence === 'secure-enclave-unlocked'

const VIA: Record<string, string> = { cli: 'CLI', dashboard: 'dashboard', phone: 'phone', factory_charter: 'the factory charter' }

/** A signature that failed to verify is said on the card itself, without opening the details. */
const sigFailed = (gate: GateStatus) => gate.approvals.some((a) => a.sig_ok === false && !phoneDecision(a))

/** Who signed, in plain words; the hash, channel and presence live under Details. */
function Approvals({ gate, viewer }: { gate: GateStatus; viewer: Viewer }) {
  // An invalidation voids the approvals (they no longer count); they stay visible, struck through, until a new one.
  const voided = gate.state === 'invalidated' && gate.approvals.length === 0 ? (gate.voided ?? []) : []
  if (gate.approvals.length === 0 && voided.length === 0) return null
  const stale = voided.length > 0
  return (
    <ul className="space-y-1">
      {(stale ? voided : gate.approvals).map((a, i) => (
        <li key={i} className={cn('text-[12px]', stale ? 'text-text-muted line-through decoration-text-faint' : 'text-text')}>
          Approved by {viewer.name(a.by)}, {fmtDay(a.at)}
          {a.via === 'factory_charter' ? ' · auto-approved under the factory charter' : ''}
          {a.source_sha ? ` · commit ${a.source_sha}` : ''}
          {' · '}
          {phoneDecision(a) ? <span className="text-text-muted">as reported by your computer</span> : a.sig_ok === undefined ? (
            <span className="text-text-muted">signature not checked</span>
          ) : (
            <span className={a.sig_ok ? 'text-success' : 'text-danger'}>{a.sig_ok ? 'verified signature' : 'signature failed'}</span>
          )}
        </li>
      ))}
    </ul>
  )
}

/** The approval evidence behind a quiet "i": policy, count, who signed, what it covers; hash and channel folded. */
function Evidence({ name, gate, viewer }: { name: GateName; gate: GateStatus; viewer: Viewer }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label="Details" className="inline-flex size-5 shrink-0 items-center justify-center rounded text-text-faint hover:text-text focus-visible:outline-2 focus-visible:outline-ring">
          <Info className="size-3.5 text-text-faint" aria-hidden />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" aria-label={`${GATE_LABEL[name]} gate`} className="w-80 space-y-2 border-border bg-surface p-3">
        <p className="text-[13px] font-semibold text-text">{GATE_LABEL[name]}</p>
        <p className="text-[12px] text-text-muted">{policyText(name, gate)}</p>
        <p className="text-[12px] text-text-muted">
          {gate.approvals.length} of {gate.needed} approvals
        </p>
        <Approvals gate={gate} viewer={viewer} />
        {gate.covers && gate.covers.length > 0 && <p className="text-[12px] text-text-muted">Covers {gate.covers.join(' · ')}</p>}
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
                    {fmtExact(a.at)}
                    {phoneDecision(a) ? ' · as reported by your computer' : <> · via {a.via ? VIA[a.via] ?? a.via : 'unknown'}
                      {a.via === 'factory_charter' ? '' : ` · presence ${a.presence ? presenceLabel(a.presence) : 'unknown'}`}
                    </>}
                  </dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </PopoverContent>
    </Popover>
  )
}

function Card({ title, text, children, gate }: { title: string; text: string; children?: ReactNode; gate?: { name: GateName; status: GateStatus; viewer: Viewer } }) {
  return (
    <div data-testid="next-step" className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-surface px-3 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-1 text-[14px] font-medium text-text">
          {title}
          {gate && <Evidence name={gate.name} gate={gate.status} viewer={gate.viewer} />}
        </p>
        <p className="text-[13px] text-text-muted">{text}</p>
        {gate && sigFailed(gate.status) && (
          <p role="alert" className="text-[12px] text-danger">
            A signature on this step failed to verify.
          </p>
        )}
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  )
}

const APPROVE_ORDER = ['requirements', 'plan', 'code'] as const
const APPROVE = {
  requirements: { title: 'Requirements ready', text: 'Read what the agent will build, then approve it or ask for a change.', button: 'Approve requirements' },
  plan: { title: 'Plan ready', text: 'Read how the agent will build it, then approve it or ask for a change.', button: 'Approve plan' },
  code: { title: 'Code review ready', text: 'Look over the changes, then approve them or ask for a change.', button: 'Approve code review' },
} as const

/** The one thing to do next, in plain words; nothing when there is nothing to do or say. */
function NextStepCard({ ticket, viewer, sign, jump, signing = false, run, wait, onFinish, onEdit, openTerminal = openDockOn }: Props) {
  const av = availableActions(ticket, viewer)
  const status = ticket.status as string
  const working = agentWorking(ticket, wait)
  const now = useNow()
  const gate = APPROVE_ORDER.find((g) => av.approve.includes(g))
  if (gate) {
    const c = APPROVE[gate]
    return (
      <Card title={c.title} text={c.text} gate={{ name: gate, status: ticket.gates[gate], viewer }}>
        <Button size="sm" disabled={signing} onClick={(e) => sign({ kind: 'approve', gate, pressed: trustedPress(e.nativeEvent) })}>
          {c.button}
        </Button>
        <Button size="sm" variant="outline" disabled={signing} onClick={() => sign({ kind: 'request_changes', gate })}>
          Change something
        </Button>
      </Card>
    )
  }
  if (av.verdict) {
    return (
      <Card title="Work is ready to check" text="Try the result and say if it does what was asked." gate={{ name: 'verify', status: ticket.gates.verify, viewer }}>
        <Button size="sm" disabled={signing} onClick={() => sign({ kind: 'verdict' })}>
          Give verdict
        </Button>
        <Button size="sm" variant="outline" disabled={signing} onClick={() => sign({ kind: 'request_changes', gate: 'verify' })}>
          Send back
        </Button>
      </Card>
    )
  }
  if (av.answer.length > 0) {
    return (
      <Card title="A question needs you" text="The agent is waiting for your answer.">
        <Button size="sm" disabled={signing} onClick={() => jump({ tab: 'questions', id: `question-${av.answer[0].id}` })}>
          Answer
        </Button>
      </Card>
    )
  }
  // The plan or requirements lack something and the agent is idle (it asks in its terminal, or its run ended): say what, and offer the way on.
  const open = (status === 'open' || status === 'in_progress' || status === 'in-progress') && !finishedOf(ticket)
  const gap = gapOf(ticket)
  const idle = !!wait || (!working && (run?.state === 'exited' || run?.state === 'lost'))
  if (gap && open && idle) {
    return (
      <Card title={gap.title} text={gap.text}>
        {onFinish && (
          <Button size="sm" onClick={onFinish}>
            Ask the agent to finish
          </Button>
        )}
        {onEdit && (
          <Button size="sm" variant="outline" onClick={onEdit}>
            Add it myself
          </Button>
        )}
      </Card>
    )
  }
  // Run-9 J04: the plan is approved and the agent is idle (its turn ended) or its run ended: one button, the build.
  if (open && !gap && idle && ticket.gates.plan?.state === 'approved' && onFinish) {
    // `idle` may come from a quiet terminal only (90 s without output): say "may be waiting", never "stopped" (Opus r9-build 2)
    return (
      <Card title="Plan approved" text="The agent may be waiting. If it does not go on by itself, start the build.">
        <Button size="sm" onClick={onFinish}>
          Start building
        </Button>
      </Card>
    )
  }
  // The agent asks something in its terminal and no gap is known: one button to it.
  if (wait && !finishedOf(ticket)) {
    return (
      <Card title="The agent is waiting for you" text="The agent is idle. Open it to see why.">
        <Button size="sm" onClick={() => openTerminal(wait)}>
          Open terminal
        </Button>
      </Card>
    )
  }
  if (working && !finishedOf(ticket)) {
    return (
      <p data-testid="next-step" className="flex items-center gap-2 px-1 text-[13px] text-text-muted">
        <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden />
        <span data-testid="activity-line">{activityLine(ticket, stepStates(ticket), run, now) ?? 'Agent is working on it'}</span>
      </p>
    )
  }
  return null
}

/** Where the ticket is (four plain steps) and the one next step for the person looking at it. */
export function TicketProgress({ ticket, viewer, sign, jump, signing = false, run, wait, onFinish, onEdit, openTerminal }: Props) {
  return (
    <div className="min-w-0 space-y-2">
      <Stepper ticket={ticket} />
      <NextStepCard ticket={ticket} viewer={viewer} sign={sign} jump={jump} signing={signing} run={run} wait={wait} onFinish={onFinish} onEdit={onEdit} openTerminal={openTerminal} />
    </div>
  )
}
