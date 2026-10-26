// "Start agent" as a split button (run 8, V2): the main part starts at once with the agent last used (no dialog when access
// is valid), the chevron "Change agent or model" opens the same dialog in choose mode. Presentational: the caller owns the dialog.
import { ChevronDown } from 'lucide-react'
import { AgentIcon } from '@/components/agents/AgentIcon'
import { Button } from '@/components/ui/button'

export function StartAgentSplit({ harness, onStart, onChoose, disabled }: { harness: string; onStart: () => void; onChoose: () => void; disabled?: boolean }) {
  return (
    <div role="group" aria-label="Start agent" data-testid="start-agent-split" className="inline-flex">
      <Button size="sm" className="rounded-r-none" disabled={disabled} data-testid="real-start-agent" onClick={onStart}>
        <AgentIcon harness={harness} size={16} />
        Start agent
      </Button>
      <Button size="sm" className="ml-px rounded-l-none px-2" disabled={disabled} aria-label="Change agent or model" data-testid="real-start-agent-choose" onClick={onChoose}>
        <ChevronDown />
      </Button>
    </div>
  )
}
