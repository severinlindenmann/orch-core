import { useQuery } from '@tanstack/react-query'
import { hubTitle } from '@/app/pages/settings/hub/shared'
import { Building2 } from 'lucide-react'
import { fetchRail, hubUrl, type RailRow } from '@/api/switch'
import { isRealHost } from '@/api/features'
import { logout } from '@/api/prodApi'
import { toastApiError } from '../toast'
import { cn } from '@/lib/utils'
import { useWorkspace } from '../workspace'
import { WorkspaceIconFor } from '../workspaceIcon'

/** The rail's data for the open workspace: the hub and the owner's workspaces, read by the server through the host's hub
 * link. Without a hub it is `{ hub: null }` and nothing is shown (no switcher without a hub). */
export function useRail() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  return useQuery({ queryKey: ['rail', ws], queryFn: () => fetchRail(ws as string), enabled: !!ws && isRealHost(), staleTime: 15_000, retry: false })
}

/** Two letters from the name ("Personal Workspace" -> "PW"); without a name the start of the id. Never "?". */
export function railInitials(r: Pick<RailRow, 'name' | 'workspace_id'>): string {
  const words = (r.name ?? '').trim().split(/\s+/).filter(Boolean)
  const s = words.length > 1 ? words[0][0] + words[1][0] : (words[0] ?? r.workspace_id).slice(0, 2)
  return s.toUpperCase()
}

/** One ticketed hop to the hub (the rail icon, the Hub page and the workspace menu all use this one way in). */
export function useOpenHub(ws: string | undefined) {
  return (returnTo: string = '/') => {
    if (!ws) return
    void hubUrl(ws, 'open', returnTo).then((u) => location.assign(u)).catch((e: unknown) => toastApiError(e, 'The hub could not be reached.'))
  }
}

/** The company rail (V3 of the hub-one-app mockup): the hub, then your workspaces. A click is one ticketed hop. */
export function CompanyRail() {
  const { workspace } = useWorkspace()
  const rail = useRail()
  const ws = workspace?.id
  const go = useOpenHub(ws)
  if (!ws || !rail.data?.hub) return null
  return (
    <nav aria-label="Company" className="flex w-14 shrink-0 flex-col items-center gap-2 border-r border-border bg-surface py-3" data-testid="company-rail">
      <button type="button" onClick={() => go('/')} aria-label={hubTitle(rail.data.hub.name)} title={hubTitle(rail.data.hub.name)}
        className="flex size-9 items-center justify-center rounded-lg bg-surface-2 text-text-muted outline-none hover:text-text focus-visible:ring-2 focus-visible:ring-brand">
        <Building2 className="size-4" aria-hidden />
      </button>
      <span className="h-px w-6 bg-border" aria-hidden />
      {rail.data.workspaces.map((r) => (
        <button key={r.workspace_id} type="button" aria-current={r.current ? 'true' : undefined} disabled={r.current}
          aria-label={`${r.name ?? 'Workspace'}${r.badge ? `, ${r.badge} need you` : ''}${r.online ? '' : ', offline'}`} title={r.name ?? 'Workspace'}
          onClick={() => go(`/open/${r.workspace_id}`)}
          className={cn('relative flex size-9 items-center justify-center rounded-lg text-[12px] font-medium outline-none focus-visible:ring-2 focus-visible:ring-brand',
            r.current ? 'bg-brand text-bg' : 'bg-surface-2 text-text hover:bg-surface-3', !r.online && 'opacity-70')}>
          {r.current && workspace ? <WorkspaceIconFor workspace={workspace} className="bg-transparent" /> : railInitials(r)}
          {r.badge > 0 && <span aria-hidden className="absolute -right-1 -top-1 min-w-4 rounded-full bg-warning px-1 text-[10px] leading-4 text-bg">{r.badge}</span>}
        </button>
      ))}
    </nav>
  )
}

/** "Log out of the hub too": a second plain click next to Log out, shown only while a hub is linked. It asks the hub for
 * a sign-out ticket, ends this session, then sends the browser to the hub, which signs itself out. */
export function HubLogout({ ws, collapsed, onDone }: { ws: string | undefined; collapsed: boolean; onDone: () => void }) {
  const rail = useRail()
  const hub = rail.data?.hub
  if (!ws || !hub) return null
  const run = async () => {
    try {
      const url = await hubUrl(ws, 'signout')
      await logout()
      onDone()
      location.assign(url)
    } catch (e) { toastApiError(e, 'Could not log out of the hub.') }
  }
  return (
    <button type="button" onClick={() => void run()}
      className={cn('rounded-md px-1.5 py-1 text-[11px] text-text-muted outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-brand', collapsed && 'hidden')}>
      Log out of {hub.name} too
    </button>
  )
}
