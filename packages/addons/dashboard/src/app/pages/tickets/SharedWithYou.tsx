import { useQuery } from '@tanstack/react-query'
import { hubQueries } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Badge } from '@/components/ui/badge'

/** Tickets other workspaces shared with this owner through the hub: read-only rows, "Shared by Anna". They are the other
 * person's tickets (not in this workspace's log), so there is nothing to open or change here. Shows nothing when the
 * workspace has no hub, the hub is away or nothing is shared (a quiet list, never an error). */
export function SharedWithYou({ ws }: { ws: string }) {
  const shared = useQuery(hubQueries.shared(ws))
  const items = shared.data?.items ?? []
  if (!items.length) return null
  return <section className="space-y-1" data-testid="shared-with-you" aria-label="Shared with you">
    <h2 className="text-[13px] font-medium text-text-muted">Shared with you</h2>
    <ul className="divide-y divide-border rounded border border-border">
      {items.map(t => <li key={t.workspace_id + t.key} className="space-y-0.5 px-3 py-2" data-testid="shared-row">
        <p className="flex flex-wrap items-center gap-2"><span className="font-mono text-[12px] text-text-faint"><Raw>{t.key}</Raw></span><Raw>{t.title}</Raw>
          <Badge variant="outline">Shared by <Raw>{t.shared_by}</Raw></Badge></p>
        {t.description && <p className="whitespace-pre-line text-[13px] text-text-muted"><Raw>{t.description}</Raw></p>}
        <p className="text-[12px] text-text-faint">{t.status.replaceAll('_', ' ')}{t.deadline ? ` · due ${t.deadline}` : ''} · read only</p>
      </li>)}
    </ul>
  </section>
}
