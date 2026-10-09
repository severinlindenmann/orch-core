import type { AddonSlot as SlotName } from '@/api/types'
import { ErrorBoundary, BlockProblem } from '@/components/ErrorBoundary'
import { AddonFrame } from './AddonFrame'
import { Skeleton } from '@/components/ui/skeleton'
import { AddonNode } from './AddonNode'
import { useSlot, type AddonStateWait, type ResolvedContribution, type SlotContext } from './slots'

/** What core draws while an addon's state is loading, or after it failed to load (with Retry). */
export function AddonStatePlaceholder({ title, waiting, compact = false }: { title: string; waiting: AddonStateWait; compact?: boolean }) {
  if (waiting.status === 'pending')
    return (
      <div role="status" aria-label={`Loading ${title}`}>
        <Skeleton className={compact ? 'h-4 w-16' : 'h-16 w-full'} />
      </div>
    )
  return (
    <div role="alert" className="flex items-center gap-2 text-[12px] text-text-muted">
      <span className="flex-1">{title} could not be loaded.</span>
      <button type="button" onClick={waiting.retry} className="rounded-md border border-border-strong px-2 py-1 text-[12px] text-text hover:bg-surface-2">
        Retry
      </button>
    </div>
  )
}

/**
 * One contribution inside its frame. `readOnly` is required: the caller derives it from the viewer's role in the
 * workspace the contribution acts in (`!can(role, 'addon.action')`, or a stricter rule such as settings).
 */
export function AddonContributionView({ c, ctx = {}, compact = false, readOnly }: { c: ResolvedContribution; ctx?: SlotContext; compact?: boolean; readOnly: boolean }) {
  return (
    <AddonFrame addon={c.addon} title={c.title} slot={c.slot} compact={compact}>
      {c.waiting ? (
        <AddonStatePlaceholder title={c.addonTitle} waiting={c.waiting} compact={compact} />
      ) : (
      <ErrorBoundary resetKey={c.node} fallback={() => <BlockProblem what={`This ${c.addon} panel`} />}>
        <AddonNode node={c.node} addon={c.addon} ctx={ctx} compact={compact} readOnly={readOnly} />
      </ErrorBoundary>
      )}
    </AddonFrame>
  )
}

/** Convenience: every contribution for a slot, stacked, each in its frame. Renders nothing when there are none. */
export function AddonSlotStack({ name, ctx = {}, readOnly, className }: { name: SlotName; ctx?: Omit<SlotContext, 'workspace'>; readOnly: boolean; className?: string }) {
  const items = useSlot(name, ctx)
  if (items.length === 0) return null
  return (
    <div className={className ?? 'space-y-3'}>
      {items.map((c) => (
        <AddonContributionView key={`${c.addon}/${c.id}`} c={c} ctx={ctx} readOnly={readOnly} />
      ))}
    </div>
  )
}
