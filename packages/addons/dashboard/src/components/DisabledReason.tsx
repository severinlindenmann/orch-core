import { cloneElement, useId, type ReactElement } from 'react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

export const VIEWER_REASON = 'Viewers cannot change tickets.'

/**
 * Explains why a control is disabled. A disabled button fires no pointer events, so the tooltip hangs off a
 * focusable span; the reason is also an sr-only description on the button. With no reason it renders the child as is.
 */
export function DisabledReason({ reason, label, children }: { reason: string | null | undefined; label?: string; children: ReactElement<Record<string, unknown>> }) {
  const id = useId()
  if (!reason) return children
  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="inline-flex rounded-md focus-visible:ring-[3px] focus-visible:ring-ring/50">
            {cloneElement(children, { disabled: true, 'aria-disabled': 'true', 'aria-describedby': id, ...(label ? { 'aria-label': label } : {}) })}
            <span id={id} className="sr-only">
              {reason}
            </span>
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs">{reason}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
