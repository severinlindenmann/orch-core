import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

/** The orange "A": marks anything an addon contributes, so people always see what is core and what is not. */
export function AddonBadge({ name, className }: { name: string; className?: string }) {
  const label = `From addon: ${name}`
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            role="img"
            aria-label={label}
            className={cn(
              'inline-flex size-4 shrink-0 select-none items-center justify-center rounded-[4px] bg-addon text-[10px] font-bold leading-none text-on-addon',
              className,
            )}
          >
            A
          </span>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
