// "Start agent" as a split button (run 8, V2): the main part starts at once with the agent last used (no dialog when access
// is valid), the chevron "Change agent or model" opens the same dialog in choose mode. The dialog comes next to the
// button that was pressed (a sheet on a phone).
import { ChevronDown } from 'lucide-react'
import { useState } from 'react'
import { AgentIcon } from '@/components/agents/AgentIcon'
import { Button } from '@/components/ui/button'
import type { Run } from '@/api/runs'
import { StartAgentDialog, lastHarness } from './StartAgentDialog'

export function StartAgentSplit({ ws, ticket, onStarted }: { ws: string; ticket: string; onStarted?: (run: Run) => void }) {
  const [mode, setMode] = useState<'start' | 'choose' | null>(null)
  return (
    <>
      <div role="group" aria-label="Start agent" data-testid="start-agent-split" className="inline-flex">
        <Button size="sm" variant="outline" className="rounded-r-none" data-testid="real-start-agent" onClick={() => setMode('start')}>
          <AgentIcon harness={lastHarness(ws)} size={16} />
          Start agent
        </Button>
        <Button size="sm" variant="outline" className="-ml-px rounded-l-none px-2" aria-label="Change agent or model" data-testid="real-start-agent-choose" onClick={() => setMode('choose')}>
          <ChevronDown />
        </Button>
      </div>
      {mode && <StartAgentDialog key={mode} ws={ws} ticket={ticket} open choose={mode === 'choose'} onClose={() => setMode(null)} onStarted={onStarted} />}
    </>
  )
}
