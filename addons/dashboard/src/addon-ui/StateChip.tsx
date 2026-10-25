import { cn } from '@/lib/utils'

/**
 * The one chip for an add-on's state, used by the install result, the Installed tab and Settings > Add-ons:
 * "On" or "Off", nothing else. Other facts about an add-on are separate badges (StateBadge), never a third state.
 */
export function StateChip({ on, className }: { on: boolean; className?: string }) {
  return (
    <span
      data-state-chip={on ? 'on' : 'off'}
      className={cn(
        'inline-flex shrink-0 select-none items-center gap-1.5 rounded-full border px-2 py-px text-[12px] font-medium leading-4',
        on ? 'border-success/40 bg-success-soft text-success' : 'border-border text-text-muted',
        className,
      )}
    >
      <span aria-hidden className={cn('size-1.5 rounded-full', on ? 'bg-success' : 'bg-text-faint')} />
      {on ? 'On' : 'Off'}
    </span>
  )
}

const BADGES = {
  approval: { text: 'Needs your approval', tone: 'border-warning/40 bg-warning-soft text-text' },
  update: { text: 'Update available', tone: 'border-border text-text-muted' },
} as const

/** A separate fact beside the On/Off chip: "Needs your approval" or "Update available". */
export function StateBadge({ kind, className }: { kind: keyof typeof BADGES; className?: string }) {
  const b = BADGES[kind]
  return <span className={cn('inline-flex shrink-0 select-none items-center rounded-full border px-2 py-px text-[12px] leading-4', b.tone, className)}>{b.text}</span>
}
