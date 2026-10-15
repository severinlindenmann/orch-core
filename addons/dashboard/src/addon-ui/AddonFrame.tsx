import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { AddonBadge } from './AddonBadge'

/** `?debug=slots` shows where a contribution comes from (package id, slot id); otherwise the header stays quiet. */
function debugSlots(): boolean {
  try {
    return new URLSearchParams(window.location.search).get('debug') === 'slots'
  } catch {
    return false
  }
}

/**
 * Wrapper for any addon-contributed element: orange hairline border, the A and the title in the header.
 * Not restylable by node props: the only inputs are the addon name, title and slot, all set by core.
 * `level` is the heading level of the title (3 inside a page, 2 where the frame is a section of its own).
 */
export function AddonFrame({
  addon,
  addonTitle,
  title,
  slot,
  actions,
  children,
  className,
  compact = false,
  level = 3,
}: {
  addon: string
  /** The addon's display title, for the A's tooltip ("From the Publish addon"). */
  addonTitle?: string
  title: string
  slot?: string
  actions?: ReactNode
  children?: ReactNode
  className?: string
  /** Badge-only frame for tiny surfaces (board card fields). */
  compact?: boolean
  level?: 2 | 3
}) {
  if (compact)
    return (
      <span data-addon={addon} title={`${addon}: ${title}`} className={cn('inline-flex items-center gap-1.5 rounded-md border border-addon-border px-1.5 py-0.5', className)}>
        <AddonBadge name={addon} title={addonTitle} className="size-3.5 text-[9px]" />
        {children}
      </span>
    )
  const Title = level === 2 ? 'h2' : 'h3'
  const debug = debugSlots()
  return (
    <section data-addon={addon} className={cn('rounded-lg border border-addon-border bg-surface', className)}>
      <header className="flex items-center gap-2 border-b border-addon-border bg-surface-2 px-3 py-2">
        <AddonBadge name={addon} title={addonTitle} />
        <Title className="flex-1 truncate text-[13px] font-semibold text-text">{title}</Title>
        {debug && (
          <span className="font-mono text-[10px] text-text-faint">
            {addon}
            {slot ? ` · ${slot}` : ''}
          </span>
        )}
        {actions}
      </header>
      <div className="p-3">{children}</div>
    </section>
  )
}
