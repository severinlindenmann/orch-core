import type { ReactNode } from 'react'
import { Info } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'

/**
 * The one place for details a normal person does not need (ids, versions, who allowed what, raw values). A small quiet
 * "i"; the content opens on press and is never shown by default. Replaces every inline "Technical details" triangle.
 * `corner` pins the button to the top right of the nearest `relative` parent.
 */
export function InfoDrawer({ children, label = 'More information', title, corner, className, testId, defaultOpen }: {
  children: ReactNode
  label?: string
  title?: string
  corner?: boolean
  className?: string
  testId?: string
  defaultOpen?: boolean
}) {
  return (
    <Popover defaultOpen={defaultOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          data-testid={testId ?? 'info-drawer'}
          className={cn(
            'inline-flex size-6 shrink-0 items-center justify-center rounded-full text-text-muted/70 hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ring',
            corner && 'absolute right-2 top-2',
            className,
          )}
        >
          <Info className="size-3.5" aria-hidden />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="max-h-[60vh] w-80 max-w-[90vw] space-y-2 overflow-y-auto text-xs text-text-muted" data-testid="info-drawer-content">
        {title && <p className="text-sm font-medium text-foreground">{title}</p>}
        {children}
      </PopoverContent>
    </Popover>
  )
}
