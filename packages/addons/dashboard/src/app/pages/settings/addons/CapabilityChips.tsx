import { Badge } from '@/components/ui/badge'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { explain } from './capabilities'

/** One chip per capability; hover or focus shows what it allows. */
export function CapabilityChips({ capabilities }: { capabilities: string[] }) {
  if (!capabilities.length) return <span className="text-[12px] text-text-faint">No capabilities</span>
  return (
    <TooltipProvider delayDuration={150}>
      <ul className="flex flex-wrap gap-1">
        {capabilities.map((c) => (
          <li key={c}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Badge variant="outline" tabIndex={0} className="font-mono text-[11px] text-text-muted">
                  {c}
                </Badge>
              </TooltipTrigger>
              <TooltipContent>{explain(c)}</TooltipContent>
            </Tooltip>
          </li>
        ))}
      </ul>
    </TooltipProvider>
  )
}
