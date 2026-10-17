// Starting a quick task as a person: ONE review and ONE confirmation (B11, order step ii). The signer signs the task
// (a chore labelled quick), its requirements, one criterion and one task per step, the plan it derives from them, the
// approval of the requirements and of the plan and, only when the box is ticked, the agent access, last. It goes through
// the workspace's ONE signing path (HandoffDialog: click where it is on, the terminal where it is off); this file adds no
// path of its own.
import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { HandoffAction } from '@/api/handoff'
import { ONE_SIGNATURE } from '@/api/quickRecords'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

/** The most a title may hold: one line (the host's own limit is wider; a quick task is one line). */
export const MAX_QUICK_TITLE = 120
export const MAX_STEPS = 4
const HOURS = [
  { hours: 8, label: '8 hours' },
  { hours: 24, label: '1 day' },
  { hours: 72, label: '3 days' },
  { hours: 168, label: '7 days' },
] as const

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

export function AddQuick({ ws }: { ws: string }) {
  const qc = useQueryClient()
  const [text, setText] = useState('')
  const [details, setDetails] = useState('')
  const [steps, setSteps] = useState<Step[]>([{ text: '', cmd: '' }])
  const [agents, setAgents] = useState(false)
  const [hours, setHours] = useState<number>(8)
  const [action, setAction] = useState<HandoffAction | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const title = oneLine(text)
  const tooLong = [...text.trim()].length > MAX_QUICK_TITLE
  const built = quickAction(text, details, steps, agents ? hours : null)
  const done = () => {
    setAction(null)
    setText('')
    setDetails('')
    setSteps([{ text: '', cmd: '' }])
    setAgents(false)
    void qc.invalidateQueries({ queryKey: ['quick', ws] })
    input.current?.focus()
  }
  const setStep = (i: number, patch: Partial<Step>) => setSteps((all) => all.map((s, n) => (n === i ? { ...s, ...patch } : s)))
  return (
    <section aria-label="Add a quick task" className="space-y-3 rounded-lg border border-border bg-surface p-3">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault()
          if (built) setAction(built)
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="quick-title">Quick task (one line)</Label>
          <Input id="quick-title" ref={input} value={text} onChange={(e) => setText(e.target.value)} aria-invalid={tooLong ? true : undefined} placeholder="Fix the typo in CONTRIBUTING" autoComplete="off" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="quick-details">Details (optional)</Label>
          <textarea id="quick-details" value={details} onChange={(e) => setDetails(e.target.value)} rows={2} className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px]" placeholder="What is wrong, and what should be true afterwards" />
        </div>
        <fieldset className="space-y-2">
          <legend className="text-[13px] font-medium text-text">Steps (optional, up to {MAX_STEPS})</legend>
          {steps.map((s, i) => (
            <div key={i} className="grid gap-2 sm:grid-cols-2">
              <Input aria-label={`Step ${i + 1}`} value={s.text} onChange={(e) => setStep(i, { text: e.target.value })} placeholder="What to do" autoComplete="off" />
              <Input aria-label={`Step ${i + 1} check`} value={s.cmd} onChange={(e) => setStep(i, { cmd: e.target.value })} placeholder="Command that checks it (optional)" autoComplete="off" className="font-mono" />
            </div>
          ))}
          {steps.length < MAX_STEPS && (
            <Button type="button" variant="ghost" size="sm" onClick={() => setSteps((all) => [...all, { text: '', cmd: '' }])}>
              Add a step
            </Button>
          )}
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-[13px] text-text">
            <input type="checkbox" checked={agents} onChange={(e) => setAgents(e.target.checked)} />
            Let agents work on workable tickets (all of them, not only this one) for
          </label>
          <select aria-label="How long agents may work" value={hours} disabled={!agents} onChange={(e) => setHours(Number(e.target.value))} className="rounded-md border border-border bg-bg px-2 py-1 text-[13px]">
            {HOURS.map((h) => (
              <option key={h.hours} value={h.hours}>
                {h.label}
              </option>
            ))}
          </select>
        </div>
        <Button type="submit" disabled={!title || !built || !!action}>
          Start task
        </Button>
      </form>
      <p data-testid="quick-one-signature" className="text-[12px] text-text-muted">
        {ONE_SIGNATURE}
      </p>
      {tooLong && (
        <p role="alert" className="text-[12px] text-danger">
          A quick task is one line of at most {MAX_QUICK_TITLE} characters.
        </p>
      )}
      {action && (
        <HandoffDialog
          title="Start task"
          ws={ws}
          action={action}
          autoStart
          onClose={() => setAction(null)}
          onSettled={(phase) => {
            if (phase === 'done') done()
          }}
        />
      )}
    </section>
  )
}
