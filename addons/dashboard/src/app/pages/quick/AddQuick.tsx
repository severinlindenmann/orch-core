// Starting a quick task as a person: ONE review and ONE confirmation (B11, order step ii). The signer signs the task
// (a chore labelled quick), its requirements, one criterion and one task per step, the plan it derives from them, the
// approval of the requirements and of the plan and, only when the box is ticked, the agent access, last. It goes through
// the workspace's ONE signing path (SignPopover: click where it is on, the terminal where it is off); this file adds no
// path of its own.
import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { HandoffAction } from '@/api/handoff'
import { accessLength, GRANT_MAX_HOURS, grantDefaultHours, runnerLengths } from '@/api/grants'
import { queries } from '@/api/queries'
import { ONE_SIGNATURE } from '@/api/quickRecords'
import { useWorkspace } from '@/app/workspace'
import { SignPopover } from '@/components/sign/SignPopover'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

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

export function AddQuick({ ws }: { ws: string }) {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  // The signer's runner grant lasts at most the workspace maximum (Settings), owners included: offer nothing longer.
  const hostCap = useQuery(queries.hostSettings(ws)).data?.grant_hours
  const cap = typeof hostCap === 'number' ? Math.min(GRANT_MAX_HOURS, Math.max(1, Math.floor(hostCap))) : grantDefaultHours(workspace)
  const lengths = runnerLengths(cap)
  const [text, setText] = useState('')
  const [details, setDetails] = useState('')
  const [steps, setSteps] = useState<Step[]>([{ text: '', cmd: '' }])
  const [agents, setAgents] = useState(false)
  const [hours, setHours] = useState<number>(8)
  const [action, setAction] = useState<HandoffAction | null>(null)
  const [checking, setChecking] = useState<number[]>([])
  const [started, setStarted] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const title = oneLine(text)
  const tooLong = [...text.trim()].length > MAX_QUICK_TITLE
  const chosen = lengths.some(([h]) => h === hours) ? hours : (lengths[0]?.[0] ?? 8)
  const built = quickAction(text, details, steps, agents ? chosen : null)
  const done = () => {
    setStarted(agents ? `An agent starts with access for ${accessLength(chosen)}.` : 'An agent picks it up when one has access.')
    setChecking([])
    setAction(null)
    setText('')
    setDetails('')
    setSteps([{ text: '', cmd: '' }])
    setAgents(false)
    void qc.invalidateQueries({ queryKey: ['quick', ws] })
    input.current?.focus()
  }
  const setStep = (i: number, patch: Partial<Step>) => setSteps((all) => all.map((s, n) => (n === i ? { ...s, ...patch } : s)))
  const filled = steps.filter((s) => s.text.trim())
  const withCheck = filled.filter((s) => s.cmd.trim()).length
  const summary = [
    title ?? 'No title yet',
    filled.length ? `${filled.length} checklist ${filled.length === 1 ? 'step' : 'steps'}${withCheck ? ` (${withCheck} with a way to check)` : ''}` : 'no checklist steps',
    agents ? `agent access for ${accessLength(chosen)}` : 'no agent access',
  ].join(' · ')
  return (
    <section aria-label="Add a quick task" className="min-w-0 space-y-4 rounded-lg border border-border bg-surface p-4">
      {started && (
        <p role="status" data-testid="quick-started" className="rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text">
          Task created and signed. {started} It is in the list below.
        </p>
      )}
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault()
          if (built) setAction(built)
        }}
      >
        <div className="space-y-1.5">
          <Label htmlFor="quick-title">Quick task (one line)</Label>
          <Input id="quick-title" ref={input} value={text} onChange={(e) => setText(e.target.value)} aria-invalid={tooLong ? true : undefined} placeholder="Fix the typo in CONTRIBUTING" autoComplete="off" className="min-h-11" />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="quick-details">Details (optional)</Label>
          <textarea id="quick-details" value={details} onChange={(e) => setDetails(e.target.value)} rows={2} className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px]" placeholder="What should be true when it is done?" />
        </div>
        <fieldset className="min-w-0 space-y-3">
          <legend className="text-[13px] font-medium text-text">Steps (optional, up to {MAX_STEPS})</legend>
          <ol className="space-y-3">
            {steps.map((s, i) => (
              <li key={i} className="min-w-0 space-y-2 rounded-lg border border-border bg-bg p-3">
                <div className="flex items-center gap-2">
                  <span aria-hidden className="grid size-6 shrink-0 place-items-center rounded-full bg-surface text-[11px] font-medium text-text-muted">{i + 1}</span>
                  <Input aria-label={`Step ${i + 1}`} value={s.text} onChange={(e) => setStep(i, { text: e.target.value })} placeholder="What to do" autoComplete="off" className="min-h-11 w-auto min-w-0 flex-1" />
                  <Button type="button" variant="ghost" size="sm" className="min-h-11 min-w-11 shrink-0 px-2 text-xs text-text-muted hover:text-text" aria-label={`Remove step ${i + 1}`} onClick={() => setSteps((all) => (all.length > 1 ? all.filter((_, n) => n !== i) : [{ text: '', cmd: '' }]))}>
                    Remove
                  </Button>
                </div>
                {checking.includes(i) || s.cmd ? (
                  <Input aria-label={`Step ${i + 1} check`} value={s.cmd} onChange={(e) => setStep(i, { cmd: e.target.value })} placeholder="Command that checks it" autoComplete="off" className="min-h-11 font-mono" />
                ) : (
                  <Button type="button" variant="ghost" size="sm" className="min-h-11 max-w-full whitespace-normal px-0 text-xs text-text-muted underline-offset-4 hover:bg-transparent hover:text-text hover:underline dark:hover:bg-transparent" onClick={() => setChecking((c) => [...c, i])}>
                    Add a way to check
                  </Button>
                )}
              </li>
            ))}
          </ol>
          {steps.length < MAX_STEPS ? (
            <Button type="button" variant="outline" className="min-h-11 w-full" onClick={() => setSteps((all) => [...all, { text: '', cmd: '' }])}>
              Add a step
            </Button>
          ) : (
            <p className="text-[12px] text-text-muted">A quick task has up to {MAX_STEPS} steps. Make a ticket for anything bigger.</p>
          )}
        </fieldset>
        <div className="space-y-3 rounded-lg border border-warning/25 bg-warning/5 p-3">
          <label className="flex items-start gap-2 text-[13px] text-text">
            <input type="checkbox" className="mt-1 size-4 shrink-0" checked={agents} onChange={(e) => setAgents(e.target.checked)} />
            <span>Let agents work on any ticket they are allowed to work on, not only this one, for</span>
          </label>
          <select aria-label="How long agents may work" value={chosen} disabled={!agents} onChange={(e) => setHours(Number(e.target.value))} className="block min-h-11 w-full min-w-0 rounded-md border border-border bg-bg px-3 text-[13px] text-text disabled:text-text-muted">
            {lengths.map(([h, label]) => (
              <option key={h} value={h}>
                {label}
              </option>
            ))}
          </select>
          <p className="text-[12px] text-text-muted">
            {agents ? `This workspace allows agent access of up to ${accessLength(cap)} (Settings).` : 'No agent access is asked for. The task waits until an agent that already has access picks it up.'}
          </p>
        </div>
        <Button type="submit" className="min-h-11 w-full" disabled={!title || !built || !!action}>
          Start task
        </Button>
        <p data-testid="quick-what-you-sign" className="break-words border-t border-border pt-3 text-[12px] leading-relaxed text-text-muted">
          <span className="font-medium">What you sign. </span>
          {summary}
        </p>
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
        <SignPopover
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
