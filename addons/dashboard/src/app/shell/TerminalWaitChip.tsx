import { SquareTerminal } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { openDockOn } from '@/app/terminal/dock/request'
import { useAttention } from '../attention'

/** Header chip of "Agent may need you: check its terminal" (run-9 feedback 3.6): the same hint as Today's row, one
 *  click from any page. Not part of the "needs you" count. */
export function TerminalWaitChip({ ws }: { ws?: string }) {
  const { terminalWaits } = useAttention(ws)
  if (terminalWaits.count === 0 || !terminalWaits.run) return null
  const run = terminalWaits.run
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" data-testid="terminal-wait-chip" onClick={() => openDockOn(run)} className="rounded focus-visible:ring-2 focus-visible:ring-ring">
          <Badge variant="outline" className="border-warning/40 bg-warning-soft text-warning">
            <SquareTerminal aria-hidden />
            Agent may need you: check its terminal{terminalWaits.count > 1 ? ` · ${terminalWaits.count}` : ''}
          </Badge>
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">Orch cannot read what the agent printed. It may be asking you something in its terminal. Click to open it.</TooltipContent>
    </Tooltip>
  )
}
