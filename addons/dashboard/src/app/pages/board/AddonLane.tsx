import { useState } from 'react'
import { cn } from '@/lib/utils'
import { useQueryClient } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { AddonBadge, openResultUrl, parseNode, useSlot, withoutReservedKeys, type ResolvedContribution } from '@/addon-ui'
import type { ItemAction } from '@/addon-ui/nodes'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { DisabledReason, VIEWER_REASON } from '@/components/DisabledReason'
import { iconByName } from '@/app/icons'
import { useWorkspace } from '@/app/workspace'
import { useRole } from '@/app/useRole'
import { can } from '@/api/permissions'
import { toastApiError } from '@/app/toast'
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
  const { workspace } = useWorkspace()
  const qc = useQueryClient()
  const canRun = can(useRole(), 'addon.action')
  const [busy, setBusy] = useState(false)
  async function run(a: ItemAction) {
    if (!workspace) return
    setBusy(true)
    try {
      const res = await api.runAddonAction(workspace.id, c.addon, a.action, withoutReservedKeys(a.args))
      toast.success(res.message)
      openResultUrl(res)
      void qc.invalidateQueries()
    } catch (e) {
      toastApiError(e, 'Action failed')
    } finally {
      setBusy(false)
    }
  }
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
        <DisabledReason reason={canRun ? null : VIEWER_REASON}>
          <div className="flex gap-1.5">
            {item.actions.map((a, i) => (
              <Button key={i} size="sm" variant="secondary" className="h-7 self-start text-[12px]" disabled={busy || !canRun} onClick={() => run(a)}>
                {a.action === 'import' && <Download className="size-3.5" />}
                {a.label}
              </Button>
            ))}
          </div>
        </DisabledReason>
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
            className={cn('flex min-h-0 w-[300px] shrink-0 flex-col rounded-lg border', addonLane)}
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
