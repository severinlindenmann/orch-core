import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { hubQueries } from '@/api/hub'
import { Name } from '@/components/sign/visible'

/** Today's small "Shared with you" card: up to 3 tickets other people shared through the hub, "See all" opens the Tickets
 * page. Same quiet rule as the Tickets section (SharedWithYou): nothing without a hub, with the hub away or when nothing
 * is shared. Shared tickets are not decisions, so Today's "Nothing needs you" line is unaffected. */
export function SharedToday({ ws }: { ws: string | undefined }) {
  const shared = useQuery({ ...hubQueries.shared(ws!), enabled: !!ws })
  const items = shared.data?.items ?? []
  if (!items.length) return null
  return (
    <section className="rounded-lg border border-border bg-surface px-3 py-2.5" data-testid="shared-today" aria-label="Shared with you">
      <h2 className="text-[13px] font-medium">Shared with you · {items.length}</h2>
      <ul className="divide-y divide-border">
        {items.slice(0, 3).map((t) => (
          <li key={t.workspace_id + t.key} className="flex items-center justify-between gap-2 py-1.5" data-testid="shared-today-row">
            <span className="min-w-0">
              <span className="block truncate text-[13px]"><Name>{t.title}</Name></span>
              <span className="block text-[12px] text-text-faint"><Name>{t.shared_by}</Name> · {t.status.replaceAll('_', ' ')}</span>
            </span>
            <Link to="/tickets" search={{ shared: `${t.workspace_id}:${t.key}` }} className="shrink-0 rounded border border-border px-2 py-0.5 text-[12px] hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-ring">Read</Link>
          </li>
        ))}
      </ul>
      <Link to="/tickets" className="mt-1 inline-block rounded text-[12px] text-text-muted hover:text-text focus-visible:ring-2 focus-visible:ring-ring">See all in Tickets</Link>
    </section>
  )
}
