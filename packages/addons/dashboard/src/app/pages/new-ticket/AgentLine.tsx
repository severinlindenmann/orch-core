// The notes of the New ticket form about the agent (no agent can start here, too many parts to sign with access), the
// dialog to choose the agent, and the "not signed" block of the review.
import { modelLabel } from '@/api/modelRouting'
import { EXECUTION_SHORT } from '@/app/terminal/real/executionLine'
import { AgentPick, harnessName, ModelPick } from '@/components/agents/AgentChoice'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { StartPlan } from './useStartPlan'

/** "Opus" or "chosen by the agent app". */
export function modelText(plan: Pick<StartPlan, 'shown' | 'choices' | 'choice'>): string {
  const { model } = plan.shown
  if (!model) return 'chosen by the agent app'
  return modelLabel(plan.choices, plan.choice.harness, model)
}

export function AgentLine({ plan, tooMany }: { plan: StartPlan; tooMany: boolean }) {
  if (!plan.feature) return null
  if (!plan.available) {
    return plan.reason ? <p className="text-[12px] text-text-muted" data-testid="agent-line-off">No agent can start here. {plan.reason} The ticket is created without starting one.</p> : null
  }
  if (!tooMany) return null
  return (
    <div className="space-y-1 text-[12px] text-text-muted" data-testid="agent-line">
      {tooMany && (
        <p role="note" className="text-warning" data-testid="agent-too-many">
          This ticket has too many parts to sign together with agent access, so it will be created without starting an agent. Start one from the ticket afterwards, or leave some text or criteria for later.
        </p>
      )}
    </div>
  )
}

export function ChangeDialog({ plan, onClose }: { plan: StartPlan; onClose: () => void }) {
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
          {plan.models.length > 0 && <ModelPick idPrefix="nt-agent" models={plan.models} model={choice.model} onModel={(model) => plan.setPref({ model })} emptyLabel={plan.routing ? 'Chosen by the model add-on' : 'Chosen by the agent app'} />}
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
export function NotSignedBlock({ plan, notice = false }: { plan: Pick<StartPlan, 'shown' | 'choices' | 'choice'>; /** Kept for callers: the steps above now say what is and is not approved. */ coreSaysSo?: boolean; /** The "runs as you" notice is not read yet: shown in full here, as the Start agent dialog would, and signing counts as reading it. */ notice?: boolean }) {
  const { choice } = plan
  return (
    <>
      <p data-testid="not-signed-start" className="border-t border-border pt-3 text-[13px] text-text-muted">
        Then {harnessName(choice.harness)} starts on it.
      </p>
      {notice && (
        <div role="note" data-testid="not-signed-notice" className="mt-2 space-y-1 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-xs leading-5 text-warning">
          {/* The short version of the line the Start agent dialog shows in full (and acknowledges as `start_agent`). */}
          <p data-testid="not-signed-execution-line">{EXECUTION_SHORT} Signing means you know this.</p>
        </div>
      )}
    </>
  )
}
