import { useEffect, useRef, useState } from 'react'
import { Link, useParams, useSearch } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { Ellipsis, Maximize, PanelRightClose, PanelRightOpen, X } from 'lucide-react'
import { AddonFrame } from '@/addon-ui/AddonFrame'
import { hasFeature, isRealHost } from '@/api/features'
import { focusRunQueries } from '@/api/focus'
import { queries } from '@/api/queries'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Conversation } from './Conversation'
import { Panel, type PanelTab } from './Panel'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { PauseButton, TasksChip } from './Delegation'
import { CreateFocusTicket, StartDialog } from './StartDialog'
import { Promote } from './Promote'

export function FocusPage() {
  usePageHeader('Focus mode')
  const { ticket } = useParams({ strict: false })
  const { scratch } = useSearch({ strict: false })
  const { workspace } = useWorkspace()
  if (!workspace || !hasFeature('focus', workspace.id)) return null
  const scratchRun = typeof scratch === 'string' ? scratch : undefined
  return <FocusSession key={ticket ?? `scratch:${scratchRun ?? ''}`} ticket={ticket} scratchRun={scratchRun} workspace={workspace.id} start={!ticket && !scratch} />
}

function FocusSession({ ticket, scratchRun, workspace, start }: { ticket?: string; scratchRun?: string; workspace: string; start: boolean }) {
  const root = useRef<HTMLDivElement>(null)
  const [fallback, setFallback] = useState(false)
  const [fullscreen, setFullscreen] = useState(false)
  const [panel, setPanel] = useState(() => window.innerWidth >= 768)
  const [tab, setTab] = useState<PanelTab>(ticket ? 'Changes' : 'Artifacts')
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
  // One answer for the header and the conversation: the ticket's own agent run decides, not the ticket's claim.
  const real = isRealHost()
  const runs = useQuery({ ...focusRunQueries.list(workspace), enabled: !!ticket, retry: false, refetchInterval: real ? 5000 : false })
  const mine = runs.data?.runs.filter((r) => r.ticket === ticket && (r.kind === 'agent' || r.as_person) && r.harness !== 'shell') ?? []
  const status = !ticket ? null : runs.isError ? 'Session unavailable' : runs.isPending ? 'Checking agent...' : mine.some((r) => r.state === 'running') ? 'Agent is working' : mine.some((r) => r.state === 'starting') ? 'Agent is starting' : 'No agent running'
  const review = () => { setTab('Decisions'); setPanel(true) }
  return (
    <div ref={root} data-testid="focus-root" className={cn('flex h-full min-h-0 min-w-0 flex-col overflow-auto rounded-lg bg-bg text-text', fallback && 'focus-fullscreen fixed inset-0 z-50 rounded-none')}>
      <header className="flex h-14 shrink-0 flex-nowrap items-center gap-2 border-b border-border bg-surface px-3 py-2 md:px-4">
        <div className="min-w-0 flex-1">
          <AddonFrame addon="focus" addonTitle="Focus mode" title="Focus mode" compact className="max-w-full min-w-0">
            {ticket ? <span data-testid="focus-ticket-chip" className="block min-w-0 truncate text-sm">{data.data ? <><span className="font-mono">{data.data.key}</span> · {data.data.title}</> : data.isError ? 'Ticket unavailable' : 'Loading ticket...'}</span> : <span className="text-sm">{start ? 'Focus mode' : 'Scratch session'}</span>}
          </AddonFrame>
        </div>
        {!ticket && !start && <Button variant="outline" size="sm" className="shrink-0" onClick={() => setCreate(true)}>Create ticket from this</Button>}
        {!start && <>
          {status && <span role="status" className="hidden shrink-0 text-xs text-text-muted md:inline">{status}</span>}
          <TasksChip ticket={ticket} />
          <div className="hidden items-center gap-2 md:flex">
            <PauseButton ticket={ticket} />
            <Button variant="ghost" size="sm" aria-pressed={fullscreen || fallback} onClick={() => void toggleFullscreen()}><Maximize />Fullscreen</Button>
            <Button variant="ghost" size="sm" aria-expanded={panel} aria-controls="focus-side-panel" onClick={() => setPanel(!panel)}>{panel ? <PanelRightClose /> : <PanelRightOpen />}{panel ? 'Hide panel' : 'Show panel'}</Button>
          </div>
          <div className="md:hidden">
            <Popover><PopoverTrigger asChild><Button variant="outline" size="icon" className="size-11" aria-label="More"><Ellipsis /></Button></PopoverTrigger>
              <PopoverContent align="end" className="w-60 space-y-2 p-3">
                {status && <p role="status" className="text-xs text-text-muted">{status}</p>}
                <PauseButton ticket={ticket} />
                <Button variant="ghost" size="sm" className="w-full justify-start" onClick={() => setPanel(true)}><PanelRightOpen />Details</Button>
                <Button variant="ghost" size="sm" className="w-full justify-start" aria-pressed={fullscreen || fallback} onClick={() => void toggleFullscreen()}><Maximize />Fullscreen</Button>
                <Button asChild variant="outline" size="sm" className="w-full">{ticket ? <Link to="/ticket/$key" params={{ key: ticket }}>Exit focus</Link> : <Link to="/">Exit focus</Link>}</Button>
              </PopoverContent></Popover>
          </div>
        </>}
        <Button asChild variant="outline" size="sm" className={cn('shrink-0', !start && 'hidden md:inline-flex')}>{ticket ? <Link to="/ticket/$key" params={{ key: ticket }}>Exit focus</Link> : <Link to="/">Exit focus</Link>}</Button>
      </header>
      {start ? <StartDialog /> : <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {ticket && data.isError ? <p role="alert" className="p-5 text-sm">Could not load this ticket. <Button variant="outline" size="sm" onClick={() => void data.refetch()}>Try again</Button></p> : <Conversation ticket={ticket} scratchRun={scratchRun} onReview={ticket ? review : undefined} />}
        </div>
        {panel && <aside id="focus-side-panel" aria-label="Session details" className="fixed inset-0 z-40 flex min-w-0 flex-col overflow-hidden bg-surface md:static md:z-auto md:w-[460px] md:max-w-[60%] md:shrink-0 md:border-l md:border-border">
          <div className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4 md:hidden"><h2 className="font-medium">Details</h2><Button variant="outline" size="sm" className="h-11" onClick={() => setPanel(false)}><X />Close</Button></div>
          <Panel ticket={ticket} scratch={!ticket} tab={tab} onTab={setTab} />
        </aside>}
      </div>}
      {create && (scratchRun ? <Promote run={scratchRun} onClose={() => setCreate(false)} /> : <CreateFocusTicket onClose={() => setCreate(false)} />)}
    </div>
  )
}
