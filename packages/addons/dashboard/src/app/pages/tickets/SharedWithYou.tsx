import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { hubQueries, type HubSharedTicket } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Badge } from '@/components/ui/badge'

/** A ticket that was in the list and is gone from the newest successful read: shown as one quiet line until dismissed. */
interface Ended { id: string; by: string; title: string }

const idOf = (t: Pick<HubSharedTicket, 'workspace_id' | 'key'>) => `${t.workspace_id}:${t.key}`
// sessionStorage, not localStorage: a shared computer must not keep ticket titles after the tab closes.
const store = (ws: string, part: 'ended' | 'last') => `orch.shared.${part}.${ws}`
function load<T>(key: string, fallback: T): T {
  try { const v = sessionStorage.getItem(key); return v ? (JSON.parse(v) as T) : fallback } catch { return fallback }
}
function save(key: string, value: unknown) {
  try { sessionStorage.setItem(key, JSON.stringify(value)) } catch { /* private window: the note just does not survive a reload */ }
}

/** Tickets other workspaces shared with this owner through the hub: read-only rows, "Shared by Anna". They are the other
 * person's tickets (not in this workspace's log), so there is nothing to open or change here. The list refreshes by itself
 * (hubQueries.shared polls); when a ticket disappears from a successful read, a plain "X stopped sharing T" line stays until
 * dismissed. A failed read never produces that note (react-query keeps the last good data and `data` does not change).
 * Shows nothing when the workspace has no hub, the hub is away or nothing is shared and nothing ended. */
export function SharedWithYou({ ws, limit }: { ws: string; limit?: number }) {
  const shared = useQuery(hubQueries.shared(ws))
  const [all, setAll] = useState(false)
  const [ended, setEnded] = useState<Ended[]>(() => load<Ended[]>(store(ws, 'ended'), []))
  const last = useRef<HubSharedTicket[] | null>(null)
  const everything = shared.data?.items
  useEffect(() => {
    if (!everything) return // no successful read yet (or never): nothing to compare
    const prev = last.current ?? load<HubSharedTicket[] | null>(store(ws, 'last'), null)
    last.current = everything
    save(store(ws, 'last'), everything)
    if (!prev) return
    const now = new Set(everything.map(idOf))
    const gone = prev.filter((t) => !now.has(idOf(t)))
    if (!gone.length) return
    setEnded((cur) => {
      const have = new Set(cur.map((e) => e.id))
      const next = [...cur, ...gone.filter((t) => !have.has(idOf(t))).map((t) => ({ id: idOf(t), by: t.shared_by, title: t.title }))]
      save(store(ws, 'ended'), next)
      return next
    })
  }, [everything, ws])
  const dismiss = (id: string) => setEnded((cur) => { const next = cur.filter((e) => e.id !== id); save(store(ws, 'ended'), next); return next })
  const list = everything ?? []
  // a ticket that is shared again is no longer "ended"
  const notes = ended.filter((e) => !list.some((t) => idOf(t) === e.id))
  if (!list.length && !notes.length) return null
  const items = limit && !all ? list.slice(0, limit) : list
  return <section className="space-y-1" data-testid="shared-with-you" aria-label="Shared with you">
    <h2 className="text-[13px] font-medium text-text-muted">Shared with you</h2>
    {notes.map((e) => <p key={e.id} role="status" data-testid="shared-ended" className="flex items-center justify-between gap-2 rounded border border-dashed border-border px-3 py-2 text-[13px] text-text-muted">
      <span><Raw>{e.by}</Raw> stopped sharing <Raw>{e.title}</Raw></span>
      <button type="button" className="text-[12px] underline" onClick={() => dismiss(e.id)}>Dismiss</button>
    </p>)}
    {list.length > 0 && <ul className="divide-y divide-border rounded border border-border">
      {items.map(t => <li key={t.workspace_id + t.key} className="space-y-0.5 px-3 py-2" data-testid="shared-row">
        <p className="flex flex-wrap items-center gap-2"><span className="font-mono text-[12px] text-text-faint"><Raw>{t.key}</Raw></span><Raw>{t.title}</Raw>
          <Badge variant="outline">Shared by <Raw>{t.shared_by}</Raw></Badge></p>
        {t.description && <p className="whitespace-pre-line text-[13px] text-text-muted"><Raw>{t.description}</Raw></p>}
        <p className="text-[12px] text-text-faint">{t.status.replaceAll('_', ' ')}{t.deadline ? ` · due ${t.deadline}` : ''} · read only</p>
      </li>)}
    </ul>}
    {limit && list.length > limit && !all && <button type="button" className="text-[12px] text-text-muted underline" onClick={() => setAll(true)}>Show all {list.length}</button>}
  </section>
}
