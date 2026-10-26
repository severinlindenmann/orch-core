import { useEffect, useRef, useState } from 'react'
import { ChevronRight } from 'lucide-react'
import type { AddonSlot as SlotName } from '@/api/types'
import { cn } from '@/lib/utils'
import { ErrorBoundary, BlockProblem } from '@/components/ErrorBoundary'
import { AddonBadge } from './AddonBadge'
import { AddonFrame } from './AddonFrame'
import { Collapse } from '@/components/Collapse'
import { Skeleton } from '@/components/ui/skeleton'
import { AddonNode, type FormControl, type Glance } from './AddonNode'
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

/** The body of a contribution: its node, or core's placeholder while the addon's state loads. */
function ContributionBody({ c, ctx, compact, readOnly, formControl, glance }: { c: ResolvedContribution; ctx: SlotContext; compact: boolean; readOnly: boolean; formControl?: FormControl; glance?: Glance }) {
  if (c.waiting) return <AddonStatePlaceholder title={c.addonTitle} waiting={c.waiting} compact={compact || !!glance} />
  return (
    <ErrorBoundary resetKey={c.node} fallback={() => <BlockProblem what={`This ${c.addon} panel`} />}>
      <AddonNode node={c.node} addon={c.addon} ctx={ctx} compact={compact} readOnly={readOnly} formControl={formControl} glance={glance} />
    </ErrorBoundary>
  )
}

/**
 * One contribution inside its frame. `readOnly` is required: the caller derives it from the viewer's role in the
 * workspace the contribution acts in (`!can(role, 'addon.action')`, or a stricter rule such as settings).
 * `bare`: the caller already shows the A and the title (an addon page's own header), so no second framed header;
 * the content keeps `data-addon` so it stays identifiable as the addon's.
 */
export function AddonContributionView({
  c,
  ctx = {},
  compact = false,
  readOnly,
  bare = false,
  level,
  formControl,
  glance,
}: {
  c: ResolvedContribution
  ctx?: SlotContext
  compact?: boolean
  readOnly: boolean
  bare?: boolean
  level?: 2 | 3
  /** A form node here draws no submit button of its own; see FormControl. */
  formControl?: FormControl
  /** Drawn as a glance line (Today's Glance list, which draws the A and the title itself); implies `bare`. */
  glance?: Glance
}) {
  const body = <ContributionBody c={c} ctx={ctx} compact={compact} readOnly={readOnly} formControl={formControl} glance={glance} />
  if (glance) return <div data-addon={c.addon}>{body}</div>
  if (bare) return <div data-addon={c.addon}>{body}</div>
  return (
    <AddonFrame addon={c.addon} addonTitle={c.addonTitle} title={c.title} slot={c.slot} compact={compact} level={level}>
      {body}
    </AddonFrame>
  )
}

const MAX_OPEN = 2
const panelKey = (c: ResolvedContribution) => `orch.panel.${c.addon}/${c.id}`
const readOpen = (c: ResolvedContribution): boolean => {
  try {
    return localStorage.getItem(panelKey(c)) === '1'
  } catch {
    return false
  }
}
const writeOpen = (c: ResolvedContribution, open: boolean) => {
  try {
    if (open) localStorage.setItem(panelKey(c), '1')
    else localStorage.removeItem(panelKey(c))
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
}

/**
 * Panels as 32 px header buttons (the A, the title, a chevron), collapsed by default. What the person opens is
 * remembered per panel, and at most two stay open: opening a third closes the one opened longest ago.
 * The panels sit in one "Addons" group (the caller's heading), so their borders are neutral: the A in each header is
 * the one addon marker per panel.
 */
export function CollapsibleStack({ items, ctx = {}, readOnly, className, level = 2 }: { items: ResolvedContribution[]; ctx?: SlotContext; readOnly: boolean; className?: string; level?: 2 | 3 | 4 }) {
  const keys = items.map(panelKey)
  const [open, setOpen] = useState<string[]>(() => items.filter(readOpen).map(panelKey).slice(-MAX_OPEN))
  const Heading = (['h2', 'h3', 'h4'] as const)[level - 2]
  /** Opens `adding` on top of `current`, closing the oldest beyond MAX_OPEN; remembers the choice. */
  const withOpened = (current: string[], adding: ResolvedContribution[]): string[] => {
    const next = [...current, ...adding.map(panelKey).filter((k) => !current.includes(k))]
    while (next.length > MAX_OPEN) {
      const dropped = next.shift() as string
      const gone = items.find((x) => panelKey(x) === dropped)
      if (gone) writeOpen(gone, false)
    }
    for (const c of adding) if (next.includes(panelKey(c))) writeOpen(c, true)
    return next
  }
  const toggle = (c: ResolvedContribution) => {
    const k = panelKey(c)
    if (open.includes(k)) {
      writeOpen(c, false)
      setOpen(open.filter((x) => x !== k))
      return
    }
    setOpen(withOpened(open, [c]))
  }
  // A panel that appears while the page is open is the result of something the person just did (e.g. "Open
  // terminal" adds a "Terminal session" panel): it opens, so the action is visibly answered. The baseline is the
  // first render where every panel's state has loaded (a waiting panel may still vanish on its `when`), and it
  // starts over for another ticket (the open set too). The seen set only grows.
  const seen = useRef<{ scope: string; keys: Set<string> } | null>(null)
  const scope = ctx.ticket?.key ?? ''
  const ready = items.every((c) => !c.waiting)
  const keyList = keys.join('\n')
  useEffect(() => {
    if (!ready) return
    if (seen.current === null || seen.current.scope !== scope) {
      // Another ticket: its panels start from what the person chose before, not from what was open on the last ticket.
      if (seen.current !== null) setOpen(items.filter(readOpen).map(panelKey).slice(-MAX_OPEN))
      seen.current = { scope, keys: new Set(keys) }
      return
    }
    const known = seen.current.keys
    const fresh = items.filter((c) => !known.has(panelKey(c)))
    // Cumulative: a panel that goes away and comes back is not new, so it keeps the person's earlier choice.
    for (const k of keys) known.add(k)
    if (fresh.length) setOpen((cur) => withOpened(cur, fresh))
    // keyList stands for `items`: only a change in which panels exist matters here.
  }, [keyList, ready, scope])
  return (
    <div className={className ?? 'space-y-2'}>
      {items.map((c, i) => {
        const isOpen = open.includes(keys[i])
        const id = `panel-${c.addon}-${c.id}`
        return (
          <section key={keys[i]} data-addon={c.addon} className="rounded-lg border border-border bg-surface">
            <Heading className="m-0">
              <button
                type="button"
                aria-expanded={isOpen}
                aria-controls={id}
                onClick={() => toggle(c)}
                className="flex h-8 w-full items-center gap-2 rounded-lg px-3 text-left text-[13px] font-semibold text-text hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-ring"
              >
                <AddonBadge name={c.addon} title={c.addonTitle} />
                <span className="flex-1 truncate">{c.title}</span>
                <ChevronRight aria-hidden className={cn('size-4 shrink-0 text-text-muted transition-transform', isOpen && 'rotate-90')} />
              </button>
            </Heading>
            <Collapse open={isOpen} id={id}>
              <div className="border-t border-border p-3">
                <ContributionBody c={c} ctx={ctx} compact={false} readOnly={readOnly} />
              </div>
            </Collapse>
          </section>
        )
      })}
    </div>
  )
}

/** Convenience: every contribution for a slot, stacked, each in its frame (or collapsible). Renders nothing when there are none. */
export function AddonSlotStack({
  name,
  ctx = {},
  readOnly,
  className,
  collapsible = false,
  level = 2,
}: {
  name: SlotName
  ctx?: Omit<SlotContext, 'workspace'>
  readOnly: boolean
  className?: string
  collapsible?: boolean
  level?: 2 | 3
}) {
  const items = useSlot(name, ctx)
  if (items.length === 0) return null
  if (collapsible) return <CollapsibleStack items={items} ctx={ctx} readOnly={readOnly} className={className} level={level} />
  return (
    <div className={className ?? 'space-y-3'}>
      {items.map((c) => (
        <AddonContributionView key={`${c.addon}/${c.id}`} c={c} ctx={ctx} readOnly={readOnly} level={level} />
      ))}
    </div>
  )
}
