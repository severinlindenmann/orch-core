import { Hourglass } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { openDockOn } from '@/app/terminal/dock/request'
import { useAttention } from '../attention'

/** Header chip of "Agent is waiting for you" (run-9 feedback 3.6, plain words since run-13 A10): the same hint as
 *  Today's row, one click from any page. Not part of the "needs you" count. */
export function TerminalWaitChip({ ws }: { ws?: string }) {
  const { terminalWaits } = useAttention(ws)
  if (terminalWaits.count === 0 || !terminalWaits.run) return null
  const run = terminalWaits.run
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button type="button" data-testid="terminal-wait-chip" onClick={() => openDockOn(run)} className="rounded focus-visible:ring-2 focus-visible:ring-ring">
          <Badge variant="outline" className="border-warning/40 bg-warning-soft text-warning">
            <Hourglass aria-hidden />
            Agent is waiting{terminalWaits.count > 1 ? ` (${terminalWaits.count})` : ''} · Open
          </Badge>
        </button>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">The agent has stopped and may need something. Click to see what.</TooltipContent>
    </Tooltip>
  )
}
