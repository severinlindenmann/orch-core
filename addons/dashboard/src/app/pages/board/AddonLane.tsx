import { useState } from 'react'
import { Download } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { AddonBadge, parseNode, useSlot, type ResolvedContribution } from '@/addon-ui'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { iconByName } from '@/app/icons'
import { useWorkspace } from '@/app/workspace'

interface LaneItem {
  title: string
  subtitle?: string
  badge?: string
}

function laneItems(c: ResolvedContribution): LaneItem[] {
  const parsed = parseNode(c.node)
  return parsed.ok && parsed.node.type === 'list' ? parsed.node.items : []
}

function LaneCard({ c, item }: { c: ResolvedContribution; item: LaneItem }) {
  const { workspace } = useWorkspace()
  const [state, setState] = useState<'idle' | 'busy' | 'done'>('idle')
  async function run() {
    setState('busy')
    try {
      const res = await api.runAddonAction(c.addon, 'import', { item, ws: workspace?.id })
      toast.success(res.message)
      setState('done')
    } catch (e) {
      setState('idle')
      toast.error(e instanceof ApiError ? e.message : 'Import failed', { description: e instanceof ApiError ? e.hint : undefined })
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
      <Button size="sm" variant="secondary" className="h-7 self-start text-[12px]" disabled={state !== 'idle'} onClick={run}>
        <Download className="size-3.5" />
        {state === 'done' ? 'Imported' : 'Import as ticket'}
      </Button>
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
            className="flex min-h-0 w-[300px] shrink-0 flex-col rounded-lg border border-addon-border bg-addon-soft/40"
          >
            <header className="flex items-center gap-2 px-3 py-2">
              <AddonBadge name={c.addon} />
              <Icon className="size-3.5 text-text-muted" aria-hidden />
              <h2 className="flex-1 truncate text-[13px] font-semibold text-text">{c.title}</h2>
              <span className="font-mono text-[11px] text-text-faint">{items.length}</span>
            </header>
            <ul className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-2 pb-2">
              {items.length === 0 ? (
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
