import { cn } from '@/lib/utils'
import { Download } from 'lucide-react'
import { AddonBadge, parseNode, useSlot, type ResolvedContribution } from '@/addon-ui'
import { roleReason, useRunAddonAction } from '@/addon-ui/useRunAddonAction'
import type { ItemAction } from '@/addon-ui/nodes'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { DisabledReason } from '@/components/DisabledReason'
import { iconByName } from '@/app/icons'
import { useRole } from '@/app/useRole'
import { addonLane } from '@/addon-ui/addonClasses'
import { AddonStatePlaceholder } from '@/addon-ui/AddonSlot'

interface LaneItem {
  title: string
  subtitle?: string
  badge?: string
  /** The item's own actions (the addon decides what a card can do, e.g. github's "Import as ticket"). */
  actions?: ItemAction[]
}

function laneItems(c: ResolvedContribution): LaneItem[] {
  const parsed = parseNode(c.node)
  return parsed.ok && parsed.node.type === 'list' ? parsed.node.items : []
}

function LaneCard({ c, item }: { c: ResolvedContribution; item: LaneItem }) {
  const r = useRunAddonAction()
  const role = useRole()
  const reasonFor = (a: ItemAction) => roleReason(role, r.meta(c.addon, a.action)?.minRole ?? 'member')
  return (
    <li className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-2.5" data-testid="lane-card">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-[13px] leading-snug text-text">{item.title}</div>
          {item.subtitle && <div className="mt-0.5 truncate font-mono text-[11px] text-text-faint">{item.subtitle}</div>}
        </div>
        {item.badge && (
          <Badge variant="outline" className="shrink-0 font-normal text-text-muted">
            {item.badge}
          </Badge>
        )}
      </div>
      {item.actions && item.actions.length > 0 && (
        <div className="flex gap-1.5">
          {r.dialog}
          {item.actions.map((a, i) => (
            <DisabledReason key={i} reason={reasonFor(a)}>
              <Button size="sm" variant="secondary" className="h-7 self-start text-[12px]" disabled={r.pending || !r.allowed(c.addon, a.action)} onClick={() => r.run(c.addon, a.action, a.args)}>
                {a.action === 'import' && <Download className="size-3.5" />}
                {a.label}
              </Button>
            </DisabledReason>
          ))}
        </div>
      )}
    </li>
  )
}

/** One extra, read-only column per `board.lane` contribution. */
export function AddonLanes() {
  const lanes = useSlot('board.lane')
  return (
    <>
      {lanes.map((c) => {
        const Icon = iconByName(c.icon)
        const items = laneItems(c)
        return (
          <section
            key={`${c.addon}/${c.id}`}
            aria-label={c.title}
            data-addon={c.addon}
            data-lane={`${c.addon}/${c.id}`}
            className={cn('relative flex min-h-0 min-w-0 flex-col rounded-lg border', addonLane)}
          >
            <header className="flex items-center gap-2 px-3 py-2">
              <AddonBadge name={c.addon} />
              <Icon className="size-3.5 text-text-muted" aria-hidden />
              <h2 className="flex-1 truncate text-[13px] font-semibold text-text">{c.title}</h2>
              <span className="font-mono text-[11px] text-text-faint">{items.length}</span>
            </header>
            <ul className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-2 pb-2">
              {c.waiting ? (
                <li className="px-2 py-2">
                  <AddonStatePlaceholder title={c.addonTitle} waiting={c.waiting} />
                </li>
              ) : items.length === 0 ? (
                <li className="px-2 py-6 text-center text-[12px] text-text-faint">Nothing to import.</li>
              ) : (
                items.map((it, i) => <LaneCard key={i} c={c} item={it} />)
              )}
            </ul>
          </section>
        )
      })}
    </>
  )
}
