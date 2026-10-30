import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { hubQueries, hubServed } from '@/api/hub'
import { isRealHost } from '@/api/features'
import { Name } from '@/components/sign/visible'
import { useMe } from '@/app/useMe'
import { kindText, spaceLabel } from '../spaces'
import { markSpacesSeen, readSeenSpaces } from '../spaces/seen'

/** Today's "New shared space" row (run-15c P03): one row for a space of the hub this workspace is in and this person
 * has not opened yet. Open goes to the Shared spaces page and clears the row (kept per person in this browser). Nothing
 * without a hub, with the hub away, or when every space was opened. Not a decision: "Nothing needs you" is unaffected. */
export function SpacesToday({ ws }: { ws: string | undefined }) {
  const me = useMe()
  const person = me.data?.person
  const list = useQuery({ ...hubQueries.spaces(ws!), enabled: isRealHost() && !!ws && hubServed(ws) })
  const [, bump] = useState(0)
  const seen = new Set(readSeenSpaces(person, ws))
  const fresh = (list.data?.spaces ?? []).filter((s) => !seen.has(s.id))
  if (!fresh.length) return null
  const first = fresh[0]
  return (
    <section className="rounded-lg border border-border bg-surface px-3 py-2.5" data-testid="spaces-today" aria-label="New shared space">
      <ul className="divide-y divide-border">
        {fresh.slice(0, 3).map((s) => (
          <li key={s.id} className="flex items-center justify-between gap-2 py-1.5" data-testid="spaces-today-row">
            <span className="min-w-0">
              <span className="block truncate text-[13px]">New shared space: <Name>{spaceLabel(s, list.data?.spaces ?? [])}</Name></span>
              <span className="block text-[12px] text-text-faint">This workspace is a member{kindText(s.kind) ? `. ${kindText(s.kind)}` : ''} · shared through the hub.</span>
            </span>
            <Link to="/spaces" onClick={() => { markSpacesSeen(person, ws, [s.id]); bump((n) => n + 1) }} className="shrink-0 rounded border border-border px-2 py-0.5 text-[12px] hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-ring">Open</Link>
          </li>
        ))}
      </ul>
      {first && fresh.length > 3 && <Link to="/spaces" className="mt-1 inline-block rounded text-[12px] text-text-muted hover:text-text">See all {fresh.length}</Link>}
    </section>
  )
}
