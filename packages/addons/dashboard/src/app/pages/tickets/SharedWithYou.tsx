import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useMe } from '@/app/useMe'
import { hubQueries, type HubSharedTicket } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Badge } from '@/components/ui/badge'

/** A ticket that was in the list and is gone from the newest successful read: shown as one quiet line until dismissed. */
type NameRow = Pick<HubSharedTicket, 'workspace_id' | 'key' | 'title' | 'shared_by'>
interface Ended { id: string; by: string; title: string }

const idOf = (t: Pick<HubSharedTicket, 'workspace_id' | 'key'>) => `${t.workspace_id}:${t.key}`
// sessionStorage, not localStorage: a shared computer must not keep ticket titles after the tab closes.
const store = (who: string, part: 'ended' | 'last') => `orch.shared.${part}.${who}`
function load<T>(key: string, fallback: T): T {
  try { const v = sessionStorage.getItem(key); return v ? (JSON.parse(v) as T) : fallback } catch { return fallback }
}
function save(key: string, value: unknown) {
  try { sessionStorage.setItem(key, JSON.stringify(value)) } catch { /* private window: the note just does not survive a reload */ }
}

/** Tickets other workspaces shared with this owner through the hub: read-only rows, "Shared by Anna". They are the other
 * person's tickets (not in this workspace's log), so there is nothing to open or change here. The list refreshes by itself
 * (hubQueries.shared polls every 5 s while the tab is visible); when a ticket disappears from a successful read, a plain "X stopped sharing T" line stays until
 * dismissed. A failed read never produces that note (react-query keeps the last good data and `data` does not change).
 * Shows nothing when the workspace has no hub, the hub is away or nothing is shared and nothing ended. */
export function SharedWithYou({ ws, limit }: { ws: string; limit?: number }) {
  const shared = useQuery(hubQueries.shared(ws))
  const [all, setAll] = useState(false)
  const last = useRef<NameRow[] | null>(null)
  const person = useMe().data?.person
  const who = person ? `${ws}:${person}` : '' // per workspace AND person: another sign-in on this tab never sees these
  const [ended, setEnded] = useState<Ended[]>([])
  useEffect(() => { last.current = null; setEnded(who ? load<Ended[]>(store(who, 'ended'), []) : []) }, [who])
  const everything = shared.data?.items
  useEffect(() => {
    if (!everything || !who) return // no successful read yet, or the person is not known: nothing to compare
    const prev = last.current ?? load<NameRow[] | null>(store(who, 'last'), null)
    // only names are kept (never a description), and only tickets someone else shared (the sharer's own rows carry no shared_by)
    const mine = everything.filter((t) => t.shared_by).map((t): NameRow => ({ workspace_id: t.workspace_id, key: t.key, title: t.title, shared_by: t.shared_by }))
    last.current = mine
    save(store(who, 'last'), mine)
    if (!prev) return
    const now = new Set(mine.map(idOf))
    const gone = prev.filter((t) => t.shared_by && !now.has(idOf(t)))
    if (!gone.length) return
    setEnded((cur) => {
      const have = new Set(cur.map((e) => e.id))
      const next = [...cur, ...gone.filter((t) => !have.has(idOf(t))).map((t) => ({ id: idOf(t), by: t.shared_by, title: t.title }))]
      save(store(who, 'ended'), next)
      return next
    })
  }, [everything, who])
  const dismiss = (id: string) => setEnded((cur) => { const next = cur.filter((e) => e.id !== id); save(store(who, 'ended'), next); return next })
  const list = everything ?? []
  // a ticket that is shared again is no longer "ended"
  const notes = ended.filter((e) => !list.some((t) => idOf(t) === e.id))
  const nav = useNavigate({ from: '/tickets' })
  const openId = useSearch({ from: '/tickets' }).shared
  const open = (id: string) => void nav({ search: (prev: Record<string, unknown>) => ({ ...prev, shared: id }) })
  const close = () => void nav({ search: (prev: Record<string, unknown>) => { const { shared: _s, ...rest } = prev; return rest } })
  const panel = openId ? list.find((t) => idOf(t) === openId) : undefined
  const goneNote = openId && !panel && everything ? notes.find((e) => e.id === openId) : undefined
  if (!list.length && !notes.length && !openId) return null
  const items = limit && !all ? list.slice(0, limit) : list
  return <section className="space-y-1" data-testid="shared-with-you" aria-label="Shared with you">
    <h2 className="text-[13px] font-medium text-text-muted">Shared with you</h2>
    {notes.map((e) => <p key={e.id} role="status" data-testid="shared-ended" className="flex items-center justify-between gap-2 rounded border border-dashed border-border px-3 py-2 text-[13px] text-text-muted">
      <span><Raw>{e.by}</Raw> stopped sharing <Raw>{e.title}</Raw></span>
      <button type="button" className="text-[12px] underline" onClick={() => dismiss(e.id)}>Dismiss</button>
    </p>)}
    {list.length > 0 && <ul className="divide-y divide-border rounded border border-border">
      {items.map(t => <li key={t.workspace_id + t.key} data-testid="shared-row">
        <button type="button" onClick={() => open(idOf(t))} aria-label={`Open ${t.key} ${t.title}`} className="block w-full space-y-0.5 px-3 py-2 text-left hover:bg-surface-hover focus-visible:ring-2 focus-visible:ring-ring">
        <p className="flex flex-wrap items-center gap-2"><span className="font-mono text-[12px] text-text-faint"><Raw>{t.key}</Raw></span><Raw>{t.title}</Raw>
          <Badge variant="outline">Shared by <Raw>{t.shared_by}</Raw></Badge></p>
        {t.description && <p className="whitespace-pre-line text-[13px] text-text-muted"><Raw>{t.description}</Raw></p>}
        <p className="text-[12px] text-text-faint">{t.status.replaceAll('_', ' ')}{t.deadline ? ` · due ${t.deadline}` : ''} · view only</p>
        </button>
      </li>)}
    </ul>}
    {openId && everything && <Dialog open onOpenChange={(o) => { if (!o) close() }}>
      <DialogContent data-testid="shared-panel" showCloseButton={false}>
        {panel ? <>
          <DialogHeader>
            <p className="text-[12px] text-text-faint">Shared by <Raw>{panel.shared_by}</Raw></p>
            <DialogTitle><Raw>{panel.title}</Raw></DialogTitle>
            <DialogDescription>{panel.status.replaceAll('_', ' ')} · {panel.deadline ? `due ${panel.deadline}` : 'no due date'}</DialogDescription>
          </DialogHeader>
          {panel.description && <p className="whitespace-pre-line text-[13px]"><Raw>{panel.description}</Raw></p>}
          <p className="rounded border border-border bg-surface px-3 py-2 text-[13px] text-text-muted">This is <Raw>{panel.shared_by}</Raw>'s ticket. You can read it; only <Raw>{panel.shared_by}</Raw> can change it.</p>
        </> : <DialogHeader>
          <DialogTitle>{goneNote ? <><Raw>{goneNote.by}</Raw> stopped sharing this</> : 'This ticket is no longer shared with you'}</DialogTitle>
          <DialogDescription>It is not in your list any more.</DialogDescription>
        </DialogHeader>}
        <Button variant="outline" size="sm" onClick={close}>Close</Button>
      </DialogContent>
    </Dialog>}
    {limit && list.length > limit && !all && <button type="button" className="text-[12px] text-text-muted underline" onClick={() => setAll(true)}>Show all {list.length}</button>}
  </section>
}
