// The agent line of the New ticket form ("Agent: Claude Code · Do the work · Opus (set in Settings) · Change"), the
// access length when the person has no usable agent access yet, and the "not signed" block of the review.
import { useState } from 'react'
import { modelLabel } from '@/api/modelRouting'
import { AgentPick, harnessName, MODE_LABEL, ModelPick } from '@/components/agents/AgentChoice'
import { AgentName } from '@/components/agents/AgentIcon'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { StartPlan } from './useStartPlan'

/** "Opus (set in Settings)", "Opus", or "chosen by the agent app". */
export function modelText(plan: Pick<StartPlan, 'shown' | 'choices' | 'choice'>): string {
  const { model, source } = plan.shown
  if (!model) return 'chosen by the agent app'
  return `${modelLabel(plan.choices, plan.choice.harness, model)}${source === 'workspace' ? ' (set in Settings)' : ''}`
}

export function AgentLine({ plan, tooMany }: { plan: StartPlan; tooMany: boolean }) {
  const [open, setOpen] = useState(false)
  if (!plan.feature) return null
  if (!plan.available) {
    return plan.reason ? <p className="text-[12px] text-text-muted" data-testid="agent-line-off">No agent can start here. {plan.reason} The ticket is created without starting one.</p> : null
  }
  const { choice } = plan
  return (
    <div className="space-y-1 text-[12px] text-text-muted" data-testid="agent-line">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <AgentName harness={choice.harness} className="text-text" />{' '}
        <span>will {choice.mode === 'refine' ? 'draft a plan for you to approve' : MODE_LABEL[choice.mode].toLowerCase()}.</span>{' '}
        <button type="button" className="underline underline-offset-4" onClick={() => setOpen(true)}>Change</button>
      </p>
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
export function NotSignedBlock({ plan }: { plan: Pick<StartPlan, 'shown' | 'choices' | 'choice'>; /** Kept for callers: the steps above now say what is and is not approved. */ coreSaysSo?: boolean }) {
  const { choice } = plan
  return (
    <p data-testid="not-signed-start" className="border-t border-border pt-3 text-[13px] text-text-muted">
      Not part of what you sign: after the ticket exists, {harnessName(choice.harness)} starts ({MODE_LABEL[choice.mode].toLowerCase()}, model {modelText(plan)}). Agent access is checked again first.
    </p>
  )
}
