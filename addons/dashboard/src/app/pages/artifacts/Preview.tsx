import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { ExternalLink, X } from 'lucide-react'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import type { Artifact, ArtifactItem, Member } from '@/api/types'
import { workspaceOfTicket } from '@/api/workspaces'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { ArtifactDrawer, Viewer } from '../ticket/Artifacts'
import { agentName, ago, fmtBytes, shortHash } from '../ticket/shared'
import { byLabel } from './label'

const isHttp = (u?: string) => !!u && /^https?:\/\//i.test(u)
/** How an item opens: a web link in a new tab, an addon artifact not here (its addon shows it), the rest in the viewer. */
export const openMode = (a: ArtifactItem): 'external' | 'addon' | 'drawer' => (a.kind === 'link' && isHttp(a.url) ? 'external' : a.addon ? 'addon' : 'drawer')

/**
 * A listed artifact's content. The list carries none, so this reads the ticket (the same visibility check as the
 * ticket page) and finds the artifact with that name and sha256; agent HTML follows the ticket's workspace (G4).
 */
function useArtifactContent(item: ArtifactItem | null, members: Member[]) {
  const ticket = useQuery({ queryKey: ['ticket', item?.ticket], queryFn: () => api.getTicket(item!.ticket), enabled: !!item })
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const full = item && ticket.data?.artifacts.find((a) => a.name === item.name && a.sha256 === item.sha256)
  const agentHtml = !!item && addonActive(workspaceOfTicket(item.ticket, workspaces.data ?? []), 'widgets')
  // Until the ticket arrives the viewer shows the listed facts without content.
  const shown: Artifact | null = item ? (full ?? { ...item, added_by: byLabel(item.by, members) }) : null
  return { shown, agentHtml, loading: !!item && !full && !ticket.isError, failed: ticket.isError }
}

function OnTicket({ item, members, failed }: { item: ArtifactItem; members: Member[]; failed: boolean }) {
  return (
    <p className="text-[12px] text-text-muted">
      On{' '}
      <Link to="/ticket/$key" params={{ key: item.ticket }} className="font-mono text-text hover:underline">
        {item.ticket}
      </Link>{' '}
      {item.ticket_title} · added by {byLabel(item.by, members)}
      {failed && <span role="alert"> · The ticket could not be loaded, so its content is not shown.</span>}
    </p>
  )
}

/** Opens a listed artifact in the ticket page's drawer (the list below 1280 px, and the grid). */
export function ArtifactPreview({ item, members, onClose, opener }: { item: ArtifactItem | null; members: Member[]; onClose: () => void; opener: { current: HTMLElement | null } }) {
  const { shown, agentHtml, loading, failed } = useArtifactContent(item, members)
  return <ArtifactDrawer artifact={shown} agentHtml={agentHtml} onClose={onClose} opener={opener} loading={loading} context={item && <OnTicket item={item} members={members} failed={failed} />} />
}

/**
 * The list view's preview beside the table (page ≥ 1280 px): the selected row's content in the same viewer as the
 * drawer, so the sandbox and agent-HTML rules are the drawer's. A web link or an addon's artifact says where it opens.
 */
export function ArtifactPane({ item, members, onClose }: { item: ArtifactItem; members: Member[]; onClose: () => void }) {
  const mode = openMode(item)
  const { shown, agentHtml, loading, failed } = useArtifactContent(mode === 'drawer' ? item : null, members)
  const a = shown ?? { ...item, added_by: byLabel(item.by, members) }
  return (
    <aside aria-label={`Preview of ${item.name}`} className="sticky top-4 flex max-h-[calc(100vh-8rem)] min-w-0 flex-col rounded-lg border border-border bg-surface">
      <header className="space-y-1 border-b border-border px-3 py-2.5">
        <div className="flex items-start gap-2">
          <h2 className="min-w-0 flex-1 break-all font-mono text-[13px] font-semibold text-text">{item.name}</h2>
          <Button type="button" variant="ghost" size="sm" className="-mr-1 -mt-1 h-7 px-1.5" aria-label="Close the preview" onClick={onClose}>
            <X aria-hidden />
          </Button>
        </div>
        <p className="text-[12px] text-text-muted">
          {a.kind} · {fmtBytes(a.bytes)} · sha256 {shortHash(a.sha256, 12)} · by {a.added_by === 'host' ? 'orch' : agentName(a.added_by)} · {ago(a.at)}
          {a.ac && ` · proves ${a.ac}`}
        </p>
        <OnTicket item={item} members={members} failed={failed} />
      </header>
      {/* The pane is the viewer's own scroller: wide datasets and logs scroll here, never the page (N11). */}
      <div className="min-h-0 flex-1 overflow-auto p-3" data-scroll-x>
        {mode === 'external' ? (
          <a href={item.url} target="_blank" rel="noopener noreferrer nofollow" className="inline-flex items-center gap-1 text-[13px] text-brand hover:underline">
            Open the link in a new tab
            <ExternalLink className="size-3" aria-hidden />
          </a>
        ) : mode === 'addon' ? (
          <p className="text-[13px] text-text-muted">The {item.addon} addon shows this artifact on its own page.</p>
        ) : loading ? (
          <Skeleton className="h-60 w-full" aria-label="Loading the artifact" />
        ) : (
          <Viewer key={a.name + a.sha256} a={a} agentHtml={agentHtml} />
        )}
      </div>
    </aside>
  )
}
