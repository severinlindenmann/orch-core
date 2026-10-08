import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'

/** Wrapper for any addon-contributed element: subtle addon border, badge in the header. */
export function AddonFrame({
  addon,
  title,
  actions,
  children,
  className,
}: {
  addon: string
  title: string
  actions?: ReactNode
  children?: ReactNode
  className?: string
}) {
  return (
    <section
      data-addon={addon}
      className={cn('rounded-lg border border-addon-border bg-surface', className)}
    >
      <header className="flex items-center gap-2 border-b border-addon-border/60 bg-addon-soft px-3 py-2">
        <AddonBadge name={addon} />
        <h3 className="flex-1 truncate text-[13px] font-semibold text-text">{title}</h3>
        {actions}
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}
