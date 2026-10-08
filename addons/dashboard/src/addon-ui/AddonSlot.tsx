import type { AddonSlot as SlotName } from '@/api/types'
import { AddonFrame } from './AddonFrame'
import { AddonNode } from './AddonNode'
import { useSlot, type ResolvedContribution, type SlotContext } from './slots'

/** One contribution inside its frame. */
export function AddonContributionView({ c, ctx = {}, compact = false }: { c: ResolvedContribution; ctx?: SlotContext; compact?: boolean }) {
  return (
    <AddonFrame addon={c.addon} title={c.title} slot={c.slot} compact={compact}>
      <AddonNode node={c.node} addon={c.addon} ctx={ctx} compact={compact} />
    </AddonFrame>
  )
}

/** Convenience: every contribution for a slot, stacked, each in its frame. Renders nothing when there are none. */
export function AddonSlotStack({ name, ctx = {}, className }: { name: SlotName; ctx?: Omit<SlotContext, 'workspace'>; className?: string }) {
  const items = useSlot(name, ctx)
  if (items.length === 0) return null
  return (
    <div className={className ?? 'space-y-3'}>
      {items.map((c) => (
        <AddonContributionView key={`${c.addon}/${c.id}`} c={c} ctx={ctx} />
      ))}
    </div>
  )
}
