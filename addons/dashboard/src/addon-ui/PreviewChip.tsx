import { cn } from '@/lib/utils'
import { useAddons } from './slots'

/**
 * "Preview": core's marker for an addon that is not part of the shipped scope yet. It is driven by the package field
 * `preview` and drawn by core next to the title (nav entry, page title, addon manager); an addon cannot draw or move it.
 * Neutral outline, never orange (orange is reserved for the A badge).
 */
export function useIsPreview(name: string): boolean {
  const { data } = useAddons()
  return !!data?.find((a) => a.name === name)?.preview
}

export function PreviewChip({ name, className }: { name: string; className?: string }) {
  const preview = useIsPreview(name)
  if (!preview) return null
  return (
    <span className={cn('inline-flex shrink-0 select-none items-center rounded-full border border-border px-1.5 py-px text-[10px] font-medium leading-4 text-text-muted', className)}>
      Preview
    </span>
  )
}
