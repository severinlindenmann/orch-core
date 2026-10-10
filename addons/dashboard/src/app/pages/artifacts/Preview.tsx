import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import type { ReactNode } from 'react'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import { ApiError, type Artifact, type ArtifactItem, type Member } from '@/api/types'
import { workspaceOfTicket } from '@/api/workspaces'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ArtifactDrawer, ArtifactFacts, Viewer } from '../ticket/Artifacts'
import { fmtBytes, Pill } from '../ticket/shared'
import { byLabel } from './label'
import { TicketLink } from './views'

export { openMode } from '../ticket/Artifacts'

type State = 'loading' | 'ready' | 'missing' | 'failed' | 'denied'

/**
 * A listed artifact's content. The list carries none, so this reads the ticket (the same visibility check as the
 * ticket page) and finds the artifact with that name and sha256; agent HTML follows the ticket's workspace (G4).
 * Fails closed: an error never shows cached content, and a ticket the viewer may no longer see shows nothing about it.
 */
function useArtifactContent(item: ArtifactItem | null) {
  const ticket = useQuery({ queryKey: ['ticket', item?.ticket], queryFn: () => api.getTicket(item!.ticket), enabled: !!item })
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const agentHtml = !!item && addonActive(workspaceOfTicket(item.ticket, workspaces.data ?? []), 'widgets')
  const err = ticket.error
  const state: State = err
    ? err instanceof ApiError && (err.status === 403 || err.status === 404)
      ? 'denied'
      : 'failed'
    : !ticket.data
      ? 'loading'
      : ticket.data.artifacts.some((a) => item && a.name === item.name && a.sha256 === item.sha256)
        ? 'ready'
        : 'missing'
  const full = state === 'ready' ? (ticket.data!.artifacts.find((a) => a.name === item!.name && a.sha256 === item!.sha256) ?? null) : null
  return { full, agentHtml, state, retry: () => void ticket.refetch() }
}

/** What the viewer area shows for each state; `null` when the artifact itself is shown. */
function stateBody(state: State, retry: () => void): ReactNode {
  if (state === 'loading') return <Skeleton className="h-60 w-full" aria-label="Loading the artifact" />
  if (state === 'missing') return <p className="text-[13px] text-text-muted">This artifact is no longer on the ticket.</p>
  if (state === 'denied') return <p className="text-[13px] text-text-muted">You can no longer see this artifact.</p>
  if (state === 'failed')
    return (
      <div role="alert" className="space-y-2 text-[13px] text-text-muted">
        <p>The ticket could not be loaded, so the artifact is not shown.</p>
        <Button type="button" variant="outline" size="sm" onClick={retry}>
          Try again
        </Button>
      </div>
    )
  return null
}

/** The listed facts, until (or instead of) the ticket's own copy. */
const listed = (item: ArtifactItem, members: Member[]): Artifact => ({ ...item, added_by: byLabel(item.by, members) })

/** The preview as a drawer: a page too narrow for the pane (DECISIONS-LOG F2, G3). */
export function ArtifactPreview({ item, members, onClose, opener }: { item: ArtifactItem | null; members: Member[]; onClose: () => void; opener: { current: HTMLElement | null } }) {
  const { full, agentHtml, state, retry } = useArtifactContent(item)
  const shown = item ? (full ?? listed(item, members)) : null
  return (
    <ArtifactDrawer
      artifact={shown}
      agentHtml={agentHtml}
      onClose={onClose}
      opener={opener}
      denied={state === 'denied'}
      by={item ? byLabel(item.by, members) : undefined}
      // Denied: the header already says it; the body stays empty.
      body={state === 'denied' ? <></> : (stateBody(state, retry) ?? undefined)}
      context={item && <TicketLink a={item} className="text-[12px]" />}
    />
  )
}

/**
 * The preview beside the results (a page wide enough): the same viewer as the drawer, so the sandbox and agent-HTML
 * rules are the drawer's.
 */
export function ArtifactPane({ item, members, onClose }: { item: ArtifactItem; members: Member[]; onClose: () => void }) {
  const { full, agentHtml, state, retry } = useArtifactContent(item)
  const a = full ?? listed(item, members)
  const denied = state === 'denied'
  return (
    <aside aria-label={denied ? 'Preview' : `Preview of ${item.name}`} className="sticky top-4 flex max-h-[calc(100vh-8rem)] min-w-0 flex-col rounded-lg border border-border bg-surface">
      <header className="space-y-1 border-b border-border px-3 py-2.5">
        <div className="flex items-start gap-2">
          <h2 className="min-w-0 flex-1 break-all font-mono text-[13px] font-semibold text-text">{denied ? 'Artifact not available' : item.name}</h2>
          <Button type="button" variant="ghost" size="sm" className="-mr-1 -mt-1 h-7 px-1.5" aria-label="Close the preview" onClick={onClose}>
            <X aria-hidden />
          </Button>
        </div>
        {!denied && (
          <>
            <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 text-[12px] text-text-muted">
              <Pill>{a.kind}</Pill>
              {a.bytes > 0 && <span>{fmtBytes(a.bytes)}</span>}
              <TicketLink a={item} className="min-w-0 flex-1" />
            </div>
            <ArtifactFacts a={a} by={byLabel(item.by, members)} />
          </>
        )}
      </header>
      {/* The pane is the viewer's own scroller: wide datasets and logs scroll here, never the page (N11). */}
      <div className="min-h-0 flex-1 overflow-auto p-3" data-scroll-x>
        {stateBody(state, retry) ?? <Viewer key={a.name + a.sha256} a={a} agentHtml={agentHtml} />}
      </div>
    </aside>
  )
}
