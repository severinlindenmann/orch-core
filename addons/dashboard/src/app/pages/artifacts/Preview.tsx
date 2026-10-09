import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { addonActive } from '@/api/addons'
import { api } from '@/api/client'
import type { ArtifactItem, Member } from '@/api/types'
import { workspaceOfTicket } from '@/api/workspaces'
import { ArtifactDrawer } from '../ticket/Artifacts'
import { byLabel } from './label'

/**
 * Opens a listed artifact in the ticket page's drawer. The list carries no content, so the drawer reads the ticket
 * (the same visibility check as the ticket page) and shows the artifact with that name and sha256.
 */
export function ArtifactPreview({ item, members, onClose, opener }: { item: ArtifactItem | null; members: Member[]; onClose: () => void; opener: { current: HTMLElement | null } }) {
  const ticket = useQuery({ queryKey: ['ticket', item?.ticket], queryFn: () => api.getTicket(item!.ticket), enabled: !!item })
  const workspaces = useQuery({ queryKey: ['workspaces'], queryFn: api.getWorkspaces })
  const full = item && ticket.data?.artifacts.find((a) => a.name === item.name && a.sha256 === item.sha256)
  const agentHtml = !!item && addonActive(workspaceOfTicket(item.ticket, workspaces.data ?? []), 'widgets')
  // Until the ticket arrives the drawer shows the listed facts without content.
  const shown = item ? (full ?? { ...item, added_by: byLabel(item.by, members) }) : null
  return (
    <ArtifactDrawer
      artifact={shown}
      agentHtml={agentHtml}
      onClose={onClose}
      opener={opener}
      loading={!!item && !full && !ticket.isError}
      context={
        item && (
          <p className="text-[12px] text-text-muted">
            On{' '}
            <Link to="/ticket/$key" params={{ key: item.ticket }} className="font-mono text-text hover:underline">
              {item.ticket}
            </Link>{' '}
            {item.ticket_title} · added by {byLabel(item.by, members)}
            {ticket.isError && <span role="alert"> · The ticket could not be loaded, so its content is not shown.</span>}
          </p>
        )
      }
    />
  )
}
