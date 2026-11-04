// Starting a quick task (run 8, V2): one input and one button. "Sign and start task" creates the task, approves its
// requirements and plan (made from the checks) in ONE signature, and then starts an agent with the defaults. The review
// opens next to the button (SignPopover); access is a select inside it only when there is none or it is nearly over.
// "Create without starting" is the same signature without the agent. No size is asked: the host measures it later.
import { useQueryClient } from '@tanstack/react-query'
import { ChevronRight } from 'lucide-react'
import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { HARNESSES } from '@/api/agents'
import type { HandoffAction } from '@/api/handoff'
import { runnerLengths } from '@/api/grants'
import { newIdem } from '@/api/runs'
import { useMe } from '@/app/useMe'
import { useWorkspace } from '@/app/workspace'
import { shownModel, type Reviewed } from '@/app/pages/new-ticket/startAfterCreate'
import { useStartPlan } from '@/app/pages/new-ticket/useStartPlan'
import { startInBackground } from '@/app/pages/ticket/progress'
import { AccessSelect } from '@/components/sign/AccessSelect'
import { SignButton } from '@/components/sign/SignButton'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { quickKey } from './status'

/** The most a title may hold: one line (the host's own limit is wider; a quick task is one line). */
export const MAX_QUICK_TITLE = 120
export const MAX_STEPS = 4

export const oneLine = (v: string): string | null => {
  const t = v.trim()
  return t && !/[\r\n]/.test(t) && [...t].length <= MAX_QUICK_TITLE ? t : null
}

interface Step {
  text: string
  cmd: string
}

/** What the form asks the signer for: only what the person typed; the plan and the criteria are derived, never typed. */
export function quickAction(title: string, description: string, steps: Step[], agentHours: number | null): HandoffAction | null {
  const t = oneLine(title)
  if (!t) return null
  const checks = steps
    .map((s) => ({ text: s.text.trim(), cmd: s.cmd.trim() }))
    .filter((s) => s.text)
    .map((s) => ({ text: s.text, ...(s.cmd ? { cmd: s.cmd } : {}) }))
  if (steps.some((s) => /[\r\n]/.test(s.text) || /[\r\n]/.test(s.cmd))) return null
  return {
    action: 'quick_start',
    title: t,
    ...(description.trim() ? { description: description.trim() } : {}),
    ...(checks.length ? { checks } : {}),
    ...(agentHours ? { agent: { hours: agentHours } } : {}),
  }
}

interface Pending {
  n: number
  action: HandoffAction
  startNow: boolean
  withAccess: boolean
  title: string
  /** What the form held when the person pressed the button: the form is cleared at once, so the popover reads from here. */
  text: string
  details: string
  steps: Step[]
  reviewed: Reviewed
  idem: string
  priorGrantIds: string[]
}

export function AddQuick({ ws, onCreated, onStartFailed, onStartDone }: { ws: string; /** A task now exists (and an agent is being started for it). */ onCreated?: (key: string, title: string) => void; onStartFailed?: (key: string) => void; /** The start finished, one way or the other. */ onStartDone?: (key: string) => void }) {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const me = useMe(ws).data
  const plan = useStartPlan(workspace!, me?.person ?? '')
  const [text, setText] = useState('')
  const [details, setDetails] = useState('')
  const [steps, setSteps] = useState<Step[]>([{ text: '', cmd: '' }])
  const [more, setMore] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  const [signing, setSigning] = useState(false)
  const [hours, setHours] = useState<number | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const startBtn = useRef<HTMLButtonElement>(null)
  const plainBtn = useRef<HTMLButtonElement>(null)
  const doneRef = useRef(false)
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const title = oneLine(text)
  const tooLong = [...text.trim()].length > MAX_QUICK_TITLE
  const canStart = plan.available
  const hoursNow = hours ?? plan.hours
  const lengths = runnerLengths(plan.cap)

  const reset = () => {
    setText('')
    setDetails('')
    setSteps([{ text: '', cmd: '' }])
    setMore(false)
    setHours(null)
    input.current?.focus()
  }

  // The signature was cancelled or failed: give the person back what they typed, unless they started something new.
  const restore = (p: Pending) => {
    if (doneRef.current || text) return
    setText(p.text)
    setDetails(p.details)
    setSteps(p.steps)
    setMore(!!p.details || p.steps.some((s) => s.text || s.cmd))
    input.current?.focus()
  }

  const begin = (start: boolean, el: HTMLElement | null) => {
    const fresh = plan.grantNow()
    const startNow = start && canStart
    const withAccess = startNow && !fresh
    const action = quickAction(text, details, steps, withAccess ? hoursNow : null)
    if (!action || !title) return
    // A quick task is a piece of work: the agent does it (not "write the requirements"), with the defaults of this workspace.
    const choice = { harness: plan.pref.harness, mode: 'work' as const, model: plan.pref.model }
    const reviewed: Reviewed = { choice, model: shownModel(choice), grantId: fresh?.id ?? null }
    setAnchor(el)
    doneRef.current = false
    setPending({ n: (pending?.n ?? 0) + 1, action, startNow, withAccess, title, text, details, steps, reviewed, idem: newIdem(), priorGrantIds: plan.priorGrantIds })
    reset()
  }

  const settled = (p: Pending, key: string | null, deposit: string | undefined) => {
    void qc.invalidateQueries({ queryKey: ['quick', ws] })
    if (!p.startNow) return toast.success('Task created')
    if (!key) return toast.warning('Task created. The agent was not started: the host did not say which task it is.')
    onCreated?.(key, p.title)
    if (p.withAccess && deposit !== undefined && deposit !== 'confirmed') {
      onStartFailed?.(key)
      return toast.warning('Task created. The agent was not started: its access is not confirmed yet.')
    }
    void startInBackground(qc, { ws, key, reviewed: p.reviewed, idem: p.idem, priorGrantIds: p.priorGrantIds }).then((out) => {
      onStartDone?.(key)
      if (out.kind === 'ask') onStartFailed?.(key)
    })
  }

  const setStep = (i: number, patch: Partial<Step>) => setSteps((all) => all.map((s, n) => (n === i ? { ...s, ...patch } : s)))
  return (
    <section aria-label="Add a quick task" className="min-w-0 space-y-3 rounded-lg border border-border bg-surface p-4">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (title && !pending) begin(true, startBtn.current)
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="quick-title">What needs doing?</Label>
          <Input id="quick-title" ref={input} value={text} onChange={(e) => setText(e.target.value)} aria-invalid={tooLong ? true : undefined} placeholder="Rename the team photo folder" autoComplete="off" className="min-h-11" />
        </div>
        <details open={more} onToggle={(e) => setMore(e.currentTarget.open)} className="group text-[13px]">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-text-muted hover:text-text [&::-webkit-details-marker]:hidden">
            <ChevronRight aria-hidden className="size-3.5 transition-transform group-open:rotate-90" /> Add details or checks
          </summary>
          <div className="mt-3 space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="quick-details">Details (optional)</Label>
              <textarea id="quick-details" value={details} onChange={(e) => setDetails(e.target.value)} rows={2} className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px]" placeholder="What should be true when it is done?" />
            </div>
            <fieldset className="min-w-0 space-y-2">
              <legend className="text-[13px] font-medium text-text">Checks (optional, up to {MAX_STEPS})</legend>
              <ol className="space-y-2">
                {steps.map((s, i) => (
                  <li key={i} className="min-w-0 space-y-2 rounded-lg border border-border bg-bg p-3">
                    <div className="flex items-center gap-2">
                      <span aria-hidden className="grid size-6 shrink-0 place-items-center rounded-full bg-surface text-[11px] font-medium text-text-muted">{i + 1}</span>
                      <Input aria-label={`Check ${i + 1}`} value={s.text} onChange={(e) => setStep(i, { text: e.target.value })} placeholder="What to check" autoComplete="off" className="min-h-11 w-auto min-w-0 flex-1" />
                      <Button type="button" variant="ghost" size="sm" className="min-h-11 min-w-11 shrink-0 px-2 text-xs text-text-muted hover:text-text" aria-label={`Remove check ${i + 1}`} onClick={() => setSteps((all) => (all.length > 1 ? all.filter((_, n) => n !== i) : [{ text: '', cmd: '' }]))}>
                        Remove
                      </Button>
                    </div>
                    {s.cmd || s.text ? <Input aria-label={`Check ${i + 1} command`} value={s.cmd} onChange={(e) => setStep(i, { cmd: e.target.value })} placeholder="Command that checks it (optional)" autoComplete="off" className="min-h-11 font-mono" /> : null}
                  </li>
                ))}
              </ol>
              {steps.length < MAX_STEPS ? (
                <Button type="button" variant="outline" className="min-h-11 w-full" onClick={() => setSteps((all) => [...all, { text: '', cmd: '' }])}>Add a check</Button>
              ) : (
                <p className="text-[12px] text-text-muted">A quick task has up to {MAX_STEPS} checks. Make a ticket for anything bigger.</p>
              )}
            </fieldset>
          </div>
        </details>
        {tooLong && <p role="alert" className="text-[12px] text-danger">A quick task is one line of at most {MAX_QUICK_TITLE} characters.</p>}
        <div className="flex flex-wrap items-center gap-2">
          <SignButton
            ref={startBtn}
            label={canStart || plan.pending ? 'Sign and start task' : 'Sign and create task'}
            disabled={!title || !!pending && !signing}
            signing={signing}
            className="min-h-11"
            data-testid="quick-start"
            onClick={() => begin(true, startBtn.current)}
          />
          {(canStart || plan.pending) && (
            <Button ref={plainBtn} type="button" variant="ghost" className="min-h-11 text-text-muted" disabled={!title || !!pending} onClick={() => begin(false, plainBtn.current)}>
              Create without starting
            </Button>
          )}
        </div>
        {!canStart && !plan.pending && plan.reason && <p className="text-[12px] text-text-muted" data-testid="quick-no-agent">No agent can start here: {plan.reason}</p>}
      </form>
      {pending && (
        <SignPopover
          key={`${pending.n}-${pending.withAccess ? hoursNow : 0}`}
          title={pending.startNow ? 'Start task' : 'Create task'}
          ws={ws}
          action={pending.withAccess ? quickAction(pending.text, pending.details, pending.steps, hoursNow) ?? pending.action : pending.action}
          startsAgent={pending.startNow}
          anchor={anchor}
          controls={pending.withAccess ? <AccessSelect hours={hoursNow} onHours={setHours} lengths={lengths} agentName={HARNESSES.find((h) => h.id === pending.reviewed.choice.harness)?.name} prefix={workspace?.prefix} /> : undefined}
          doneToast={pending.startNow ? '' : 'Task created'}
          onSigning={setSigning}
          onSettled={(phase, state, request) => {
            if (phase === 'done') {
              doneRef.current = true
              settled(pending, quickKey(state, request), state?.deposit)
            } else if (phase === 'failed') restore(pending)
          }}
          onClose={() => {
            restore(pending)
            setPending(null)
          }}
        />
      )}
    </section>
  )
}
