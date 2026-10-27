import { Check, Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { addFirst } from '@/api/gates'
import type { TicketDocument } from '@/api/types'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { availableActions } from './actions'
import type { HumanAction, Jump, Viewer } from './shared'

type Props = { ticket: TicketDocument; viewer: Viewer; sign: (a: HumanAction) => void; jump: (j: Jump) => void }
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

function Card({ title, text, children }: { title: string; text: string; children?: ReactNode }) {
  return (
    <div data-testid="next-step" className="flex min-w-0 flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-surface px-3 py-2.5">
      <div className="min-w-0 flex-1">
        <p className="text-[14px] font-medium text-text">{title}</p>
        <p className="text-[13px] text-text-muted">{text}</p>
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
function NextStepCard({ ticket, viewer, sign, jump }: Props) {
  const av = availableActions(ticket, viewer)
  const status = ticket.status as string
  const working = !!ticket.claim && ticket.claim.live !== false
  const gate = APPROVE_ORDER.find((g) => av.approve.includes(g))
  if (gate) {
    const c = APPROVE[gate]
    return (
      <Card title={c.title} text={c.text}>
        <Button size="sm" onClick={() => sign({ kind: 'approve', gate })}>
          {c.button}
        </Button>
        <Button size="sm" variant="outline" onClick={() => sign({ kind: 'request_changes', gate })}>
          Change something
        </Button>
      </Card>
    )
  }
  if (av.verdict) {
    return (
      <Card title="Work is ready to check" text="Try the result and say if it does what was asked.">
        <Button size="sm" onClick={() => sign({ kind: 'verdict' })}>
          Give verdict
        </Button>
        <Button size="sm" variant="outline" onClick={() => sign({ kind: 'request_changes', gate: 'verify' })}>
          Send back
        </Button>
      </Card>
    )
  }
  if (av.answer.length > 0) {
    return (
      <Card title="A question needs you" text="The agent is waiting for your answer.">
        <Button size="sm" onClick={() => jump({ tab: 'questions', id: `question-${av.answer[0].id}` })}>
          Answer
        </Button>
      </Card>
    )
  }
  // Owner feedback 5/14: the agent ended its run before the requirements or the plan were complete, and nothing said so.
  if ((status === 'open' || status === 'in_progress') && !working) {
    const stuck = (['requirements', 'plan'] as const).find((g) => ticket.gates[g].state !== 'approved' && !!ticket.gates[g].missing?.length)
    if (stuck) return <Card title="The agent stopped early" text={`${addFirst(ticket.gates[stuck].missing ?? [])} Start the agent again to finish it.`} />
  }
  if (working && !finishedOf(ticket)) {
    return (
      <p data-testid="next-step" className="flex items-center gap-2 px-1 text-[13px] text-text-muted">
        <Loader2 className="size-3.5 shrink-0 animate-spin" aria-hidden />
        Agent is working on it
      </p>
    )
  }
  return null
}

/** Where the ticket is (four plain steps) and the one next step for the person looking at it. */
export function TicketProgress({ ticket, viewer, sign, jump }: Props) {
  return (
    <div className="min-w-0 space-y-2">
      <Stepper ticket={ticket} />
      <NextStepCard ticket={ticket} viewer={viewer} sign={sign} jump={jump} />
    </div>
  )
}
