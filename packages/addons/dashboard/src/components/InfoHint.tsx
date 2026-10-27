import type { ReactNode } from 'react'
import { Info } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

/** Minimal quiet "i" with details behind it. TODO(UA): swap for the shared InfoDrawer once it is on develop. */
export function InfoHint({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" aria-label={label} title={label} className="inline-flex size-5 shrink-0 items-center justify-center rounded-full text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand">
          <Info className="size-3.5" aria-hidden />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 space-y-1 text-[13px]">{children}</PopoverContent>
    </Popover>
  )
}
