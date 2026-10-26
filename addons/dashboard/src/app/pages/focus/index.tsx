import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearch } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Maximize, PanelRightClose, PanelRightOpen } from 'lucide-react'
import { AddonFrame } from '@/addon-ui/AddonFrame'
import { hasFeature } from '@/api/features'
import { queries } from '@/api/queries'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Conversation } from './Conversation'
import { Panel } from './Panel'
import { TasksChip, TasksList } from './Delegation'
import { CreateFocusTicket, StartDialog } from './StartDialog'

export function FocusPage() {
  usePageHeader('Focus mode')
  const { ticket } = useParams({ strict: false })
  const { scratch } = useSearch({ strict: false })
  const { workspace } = useWorkspace()
  if (!workspace || !hasFeature('focus', workspace.id)) return null
  return <FocusSession key={ticket ?? 'scratch'} ticket={ticket} workspace={workspace.id} start={!ticket && scratch !== 1} />
}

function FocusSession({ ticket, workspace, start }: { ticket?: string; workspace: string; start: boolean }) {
  const root = useRef<HTMLDivElement>(null)
  const [fallback, setFallback] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [panel, setPanel] = useState(true)
  const [create, setCreate] = useState(false)
  const data = useQuery({ ...queries.ticket(workspace, ticket ?? ''), enabled: !!ticket })
  useEffect(() => {
    const changed = () => setFullscreen(document.fullscreenElement === root.current)
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setFallback(false) }
    document.addEventListener('fullscreenchange', changed)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('fullscreenchange', changed)
      document.removeEventListener('keydown', escape)
    }
  }, [])
  async function toggleFullscreen() {
    if (document.fullscreenElement) {
      await document.exitFullscreen?.()
      setFullscreen(document.fullscreenElement === root.current)
    } else if (fallback) {
      setFallback(false)
    } else {
      try {
        if (!root.current?.requestFullscreen) throw new Error('Fullscreen unavailable')
        await root.current.requestFullscreen()
        setFullscreen(document.fullscreenElement === root.current)
      } catch {
        setFallback(true)
      }
    }
  }
  const working = data.data?.claim && data.data.claim.live !== false
  return (
    <div ref={root} data-testid="focus-root" className={cn('flex h-full min-h-0 min-w-0 flex-col overflow-auto rounded-lg bg-bg text-text', fallback && 'focus-fullscreen fixed inset-0 z-50 rounded-none')}>
      <header className="flex flex-wrap items-center gap-2 border-b border-border bg-surface px-4 py-3">
        <AddonFrame addon="focus" addonTitle="Focus mode" title="Focus mode" compact>
          {ticket ? <span data-testid="focus-ticket-chip" className="break-words text-sm">{data.data ? <><span className="font-mono">{data.data.key}</span> · {data.data.title}</> : data.isError ? 'Ticket unavailable' : 'Loading ticket...'}</span> : <span className="text-sm">No ticket</span>}
        </AddonFrame>
        {!ticket && <Button variant="outline" size="sm" onClick={() => setCreate(true)}>Create ticket from this</Button>}
        <span role="status" className="text-xs text-text-muted">{working ? 'Agent is working' : 'No active session'}</span>
        <TasksChip />
        <span data-testid="focus-pause" title="Pause coming in F4"><Button variant="outline" size="sm" disabled>Pause</Button></span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <Button variant="ghost" size="sm" aria-pressed={fullscreen || fallback} onClick={() => void toggleFullscreen()}><Maximize />Fullscreen</Button>
          <Button variant="ghost" size="sm" aria-expanded={panel} aria-controls="focus-side-panel" onClick={() => setPanel(!panel)}>{panel ? <PanelRightClose /> : <PanelRightOpen />}{panel ? 'Hide panel' : 'Show panel'}</Button>
          <Button asChild variant="outline" size="sm">{ticket ? <Link to="/ticket/$key" params={{ key: ticket }}>Exit focus</Link> : <Link to="/">Exit focus</Link>}</Button>
        </div>
      </header>
      {start ? <StartDialog /> : <div className="flex min-h-0 min-w-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col overflow-auto">
          {ticket && data.isError ? <p role="alert" className="p-5 text-sm">Could not load this ticket. <Button variant="outline" size="sm" onClick={() => void data.refetch()}>Try again</Button></p> : <Conversation ticket={ticket} />}
        </div>
        {panel && <aside id="focus-side-panel" aria-label="Session details" className="flex w-[420px] max-w-[60%] min-w-0 shrink-0 flex-col overflow-auto border-l border-border bg-surface">
          <Panel />
          <TasksList />
        </aside>}
      </div>}
      {create && <CreateFocusTicket onClose={() => setCreate(false)} />}
    </div>
  )
}
