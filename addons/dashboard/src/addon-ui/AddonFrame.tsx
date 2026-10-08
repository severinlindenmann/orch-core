import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'

/**
 * Wrapper for any addon-contributed element: orange hairline border, badge + addon name + slot in the header.
 * Not restylable by node props: the only inputs are the addon name, title and slot, all set by core.
 */
export function AddonFrame({
  addon,
  title,
  slot,
  actions,
  children,
  className,
  compact = false,
}: {
  addon: string
  title: string
  slot?: string
  actions?: ReactNode
  children?: ReactNode
  className?: string
  /** Badge-only frame for tiny surfaces (board card fields). */
  compact?: boolean
}) {
  if (compact)
    return (
      <span data-addon={addon} title={`${addon}: ${title}`} className={cn('inline-flex items-center gap-1.5 rounded-md border border-addon-border px-1.5 py-0.5', className)}>
        <AddonBadge name={addon} className="size-3.5 text-[9px]" />
        {children}
      </span>
    )
  return (
    <section data-addon={addon} className={cn('rounded-lg border border-addon-border bg-surface', className)}>
      <header className="flex items-center gap-2 border-b border-addon-border/60 bg-addon-soft px-3 py-2">
        <AddonBadge name={addon} />
        <span className="font-mono text-[11px] text-text-muted">{addon}</span>
        <h3 className="flex-1 truncate text-[13px] font-semibold text-text">{title}</h3>
        {slot && <span className="font-mono text-[10px] text-text-faint">{slot}</span>}
        {actions}
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}
