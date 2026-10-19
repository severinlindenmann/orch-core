// The agent line of the New ticket form ("Agent: Claude Code · Do the work · Opus (set in Settings) · Change"), the
// access length when the person has no usable agent access yet, and the "not signed" block of the review.
import { useState } from 'react'
import { modelLabel } from '@/api/modelRouting'
import { accessLength } from '@/api/grants'
import { AgentPick, harnessName, MODE_LABEL, ModelPick } from '@/components/agents/AgentChoice'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import type { StartPlan } from './useStartPlan'

/** "Opus (set in Settings)", "Opus", or "the model the agent app chooses". */
export function modelText(plan: Pick<StartPlan, 'shown' | 'choices' | 'choice'>): string {
  const { model, source } = plan.shown
  if (!model) return 'the model the agent app chooses'
  return `${modelLabel(plan.choices, plan.choice.harness, model)}${source === 'workspace' ? ' (set in Settings)' : ''}`
}

export function AgentLine({ plan, tooMany }: { plan: StartPlan; tooMany: boolean }) {
  const [open, setOpen] = useState(false)
  if (!plan.feature) return null
  if (!plan.available) {
    return plan.reason ? <p className="mb-2 text-[12px] text-text-muted" data-testid="agent-line-off">Agent: not available. {plan.reason} The ticket is created without starting one.</p> : null
  }
  const { choice } = plan
  return (
    <div className="mb-2 space-y-1 text-[12px] text-text-muted" data-testid="agent-line">
      <p>
        <span className="text-text">Agent:</span> {harnessName(choice.harness)} <span aria-hidden>·</span> {MODE_LABEL[choice.mode]} <span aria-hidden>·</span> {modelText(plan)} <span aria-hidden>·</span>{' '}
        <button type="button" className="underline underline-offset-4" onClick={() => setOpen(true)}>Change</button>
      </p>
      {plan.needsGrant && !tooMany && (
        <p className="flex flex-wrap items-center gap-2" data-testid="agent-access">
          <Label htmlFor="nt-agent-hours" className="text-[12px] font-normal text-text-muted">Agent access (you have none right now) for</Label>
          <select id="nt-agent-hours" className="h-7 rounded border border-border bg-bg px-2 text-[12px] text-text" value={plan.hours} onChange={(e) => plan.setHours(Number(e.target.value))}>
            {plan.lengths.map(([h, label]) => <option key={h} value={h}>{label}</option>)}
          </select>
          <span>This workspace allows up to {accessLength(plan.cap)}.</span>
        </p>
      )}
      {tooMany && (
        <p role="note" className="text-warning" data-testid="agent-too-many">
          This ticket has too many parts to sign together with agent access, so it will be created without starting an agent. Start one from the ticket afterwards, or leave some text or criteria for later.
        </p>
      )}
      {open && <ChangeDialog plan={plan} onClose={() => setOpen(false)} />}
    </div>
  )
}

function ChangeDialog({ plan, onClose }: { plan: StartPlan; onClose: () => void }) {
  const { choice } = plan
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose() }}>
      <DialogContent className="max-w-[calc(100%-2rem)] gap-3 border-border bg-surface p-4 sm:max-w-md" data-testid="agent-change">
        <DialogHeader className="text-left">
          <DialogTitle>Agent for this ticket</DialogTitle>
          <DialogDescription>Remembered for this workspace. You can still change it in the Start agent dialog.</DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-1 items-center gap-x-3 gap-y-2 text-xs leading-5 sm:grid-cols-[110px_minmax(0,1fr)]">
          <AgentPick idPrefix="nt-agent" harness={choice.harness} mode={choice.mode} onHarness={(harness) => plan.setPref({ harness, model: '' })} onMode={(mode) => plan.setPref({ mode })} />
          {plan.models.length > 0 && <ModelPick idPrefix="nt-agent" models={plan.models} model={choice.model} onModel={(model) => plan.setPref({ model })} emptyLabel={plan.routing ? 'As set in Settings for this kind of work' : 'Chosen by the agent app'} />}
        </div>
        <DialogFooter><Button size="sm" onClick={onClose}>Done</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * Under the review, outside the signed content: the start is NOT part of what the person signs. It uses their agent
 * access and happens in the dashboard once the ticket exists.
 */
export function NotSignedBlock({ plan, coreSaysSo = false }: { plan: Pick<StartPlan, 'shown' | 'choices' | 'choice'>; /** The signed review already says nothing here approves the requirements or the plan (agent access is in it). */ coreSaysSo?: boolean }) {
  const { choice } = plan
  return (
    <section aria-label="Not signed: the agent start" data-testid="not-signed-start" className="space-y-1 border-t border-border pt-3 text-[13px] leading-relaxed">
      <p className="font-medium text-text">Not signed: the agent start</p>
      <p className="text-text-muted">
        After the ticket exists, this page starts an agent with your agent access: {harnessName(choice.harness)}, {MODE_LABEL[choice.mode]}, model {modelText(plan)}, in the project folder.
        The start is checked again first and only goes ahead if it is what you see here; otherwise you are asked.
        {!coreSaysSo && ' Nothing here approves the requirements or the plan: the agent will ask you to.'}
      </p>
    </section>
  )
}
