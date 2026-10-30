// Shared spaces (run-15c P03): the spaces of the hub this workspace is a member of, read-only. Adding or editing is
// done in the hub. The name and kind come from the hub and are not signed; the items were written by other workspaces
// and are shown as plain text only (never markup, never followed as instructions).
import { useQuery } from '@tanstack/react-query'
import { useEffect } from 'react'
import { hubQueries, hubServed, type HubSpace } from '@/api/hub'
import { LoadFailed } from '@/components/LoadFailed'
import { Name } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { useMe } from '@/app/useMe'
import { useWorkspace } from '@/app/workspace'
import { useOpenHub } from '@/app/shell/CompanyRail'
import { markSpacesSeen } from './seen'

export const KIND_TEXT: Record<string, string> = { wiki: 'Wiki', files: 'Files', notes: 'Notes' }
export const spaceTitle = (s: HubSpace) => s.name || 'Shared space'
/** Two spaces with the same name are told apart by the last 6 characters of their id; a unique name shows none. */
export const spaceLabel = (s: HubSpace, all: HubSpace[]) =>
  all.filter((o) => spaceTitle(o) === spaceTitle(s)).length > 1 ? `${spaceTitle(s)} (…${s.id.slice(-6)})` : spaceTitle(s)
export const kindText = (k: string) => KIND_TEXT[k] ?? (k ? k.charAt(0).toUpperCase() + k.slice(1) : '')

function fmtSize(n: number): string {
  return n < 1024 ? `${n} B` : `${Math.round(n / 1024)} KB`
}

function SpaceCard({ ws, space, all, openHub }: { ws: string; space: HubSpace; all: HubSpace[]; openHub: () => void }) {
  const items = useQuery({ ...hubQueries.spaceItems(ws, space.id) })
  const kind = kindText(space.kind)
  return (
    <section className="rounded-lg border border-border bg-surface p-4" data-testid="space-card" aria-label={spaceLabel(space, all)}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-[15px] font-medium"><Name>{spaceLabel(space, all)}</Name></h2>
        <span className="text-[12px] text-text-muted">
          {kind ? `${kind} · ` : ''}{space.members} {space.members === 1 ? 'workspace' : 'workspaces'}
          {items.data ? ` · ${items.data.items.length} ${items.data.items.length === 1 ? 'item' : 'items'}` : ''}
        </span>
      </div>
      {items.isPending ? (
        <p role="status" className="mt-2 text-[13px] text-text-muted">Reading the items…</p>
      ) : items.isError ? (
        <p role="alert" className="mt-2 text-[13px] text-text-muted">The items could not be read right now. Try again in a moment.</p>
      ) : items.data.items.length === 0 ? (
        <p className="mt-2 text-[13px] text-text-muted" data-testid="space-empty">Nothing has been added yet.</p>
      ) : (
        <ul className="mt-2 divide-y divide-border" data-testid="space-items">
          {items.data.items.map((i, n) => (
            <li key={`${i.name}-${n}`} className="flex items-center justify-between gap-3 py-1.5 text-[13px]">
              <span className="min-w-0"><Name>{i.name}</Name></span>
              <span className="shrink-0 text-[12px] text-text-faint">{fmtSize(i.size)}</span>
            </li>
          ))}
        </ul>
      )}
      {items.data && items.data.refused > 0 && (
        <p className="mt-2 text-[12px] text-text-muted">{items.data.refused} {items.data.refused === 1 ? 'item was' : 'items were'} left out because it could not be checked.</p>
      )}
      <div className="mt-3">
        <Button size="sm" variant="outline" onClick={openHub} data-testid="space-open-hub">Add or edit in the hub</Button>
      </div>
    </section>
  )
}

export function SpacesPage() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const me = useMe()
  const person = me.data?.person
  const list = useQuery({ ...hubQueries.spaces(ws ?? ''), enabled: !!ws && hubServed(ws) })
  const openHub = useOpenHub(ws)
  const ids = (list.data?.spaces ?? []).map((s) => s.id).join(',')
  // Looking at the page is opening the spaces: Today stops saying they are new.
  useEffect(() => { if (ids) markSpacesSeen(person, ws, ids.split(',')) }, [person, ws, ids])
  if (!ws) return null
  if (list.isError) return <LoadFailed what="shared spaces" detail="The hub could not be reached. Try again in a moment." onRetry={() => void list.refetch()} />
  const spaces = list.data?.spaces ?? []
  return (
    <div className="w-full max-w-3xl space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Shared spaces</h1>
      <p className="text-[13px] text-text-muted">The spaces of the hub this workspace is a member of. You can read them here; adding or editing is done in the hub. Names are from the hub, not signed.</p>
      {list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">Reading the spaces…</p>
      ) : spaces.length === 0 ? (
        <p data-testid="spaces-none" className="text-[13px] text-text-muted">This workspace is not in a shared space yet.</p>
      ) : (
        spaces.map((s) => <SpaceCard key={s.id} ws={ws} space={s} all={spaces} openHub={() => openHub('/')} />)
      )}
    </div>
  )
}
