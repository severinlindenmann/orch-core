import { useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Switch } from '@/components/ui/switch'
import { LiveDelegate } from './LiveDelegate'
import { AgentView, JobView } from './Center'
import { PreviewTag, StateDot } from './parts'
import { RequestBanner, RequestSheet } from './Request'
import { AgentTree, treeSummary } from './Tree'
import { agentById, costKind, jobById, LiveProvider, useClock, useLive, useLiveSnapshot } from './useLive'

/** Header extras of the live view: how fresh the picture is, the Details switch, and "Stop ticket work". */
export function LiveHeaderControls({ details, onDetails }: { details: boolean; onDetails: (on: boolean) => void }) {
  return <>
    <label className="flex shrink-0 items-center gap-1.5 text-xs text-text-muted" title="Show models, usage and cache">
      <Switch checked={details} onCheckedChange={onDetails} aria-label="Details" className="h-4 w-7 [&>span]:size-3 [&>span]:data-[state=checked]:translate-x-[14px]" />Details
    </label>
    <Button variant="outline" size="sm" className="hidden shrink-0 md:inline-flex" title="Stops every agent on this ticket" onClick={() => toast('Preview: this would stop every agent on the ticket. Nothing was stopped.')}>Stop ticket work</Button>
  </>
}

function ProgressLine({ details, updatedAt }: { details: boolean; updatedAt: number }) {
  const live = useLive()!
  const ago = updatedAt ? Math.max(0, Math.floor((live.now - updatedAt) / 1000)) : null
  const tasks = live.snap.agents.filter((a) => a.kind !== 'main')
  const done = tasks.filter((a) => a.state === 'done').length
  const api = live.snap.agents.filter((a) => costKind(a) === 'api').reduce((s, a) => s + a.usd, 0)
  return <div data-testid="live-progress" className="flex shrink-0 flex-wrap items-center gap-x-5 gap-y-1 border-b border-border bg-surface px-4 py-2 text-xs text-text-muted">
    <span><b className="text-text">{done} of {tasks.length}</b> tasks done</span>
    {details && <>
      <span>Estimated API charges: <b className="text-text">${api.toFixed(2)}</b></span>
      <span>Covered by subscription · {live.snap.accounts.map((a) => <span key={a.key} className="mr-2">{a.label.split(' · ')[0]} <b className="text-text">{a.pct}%</b> of allowance used</span>)}</span>
    </>}
    <span className="ml-auto flex items-center gap-2"><PreviewTag />{ago != null && <span role="status" data-testid="live-fresh" className={ago > 60 ? 'text-warning' : undefined}>{ago < 3 ? 'updated just now' : `updated ${ago} s ago`}</span>}</span>
  </div>
}

export function LiveFocus({ ws, ticket, details, side }: { ws: string; ticket: string; details: boolean; side: ReactNode }) {
  const q = useLiveSnapshot(ws, ticket)
  const now = useClock()
  const [selected, setSelected] = useState('main')
  const [request, setRequest] = useState(false)
  const [drawer, setDrawer] = useState(false)
  const snap = q.data
  if (!snap) return <div className="flex flex-1 items-center justify-center p-6 text-sm text-text-muted" role="status">{q.isError ? 'Could not load the live agents.' : 'Loading live agents...'}</div>
  const id = agentById(snap, selected) || jobById(snap, selected) ? selected : 'main'
  const ctx = { ws, ticket, snap, selected: id, select: setSelected, details, now, openRequest: () => setRequest(true) }
  const agent = agentById(snap, id)
  const job = jobById(snap, id)
  const shownAgent = agent ?? agentById(snap, job?.parent ?? 'main')!
  return <LiveProvider value={ctx}>
    <ProgressLine details={details} updatedAt={q.dataUpdatedAt} />
    <RequestBanner />
    {/* Under 768 px the tree is a drawer; the selected agent stays on screen. */}
    <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-2 md:hidden">
      <Button variant="outline" size="sm" aria-expanded={drawer} onClick={() => setDrawer(true)}>Agents <ChevronDown aria-hidden /></Button>
      <StateDot state={(agent ?? shownAgent).state} />
      <span data-testid="live-selected-summary" className="min-w-0 truncate text-xs"><b>{job?.name ?? shownAgent.name}</b> <span className="text-text-muted">· {treeSummary(snap)}</span></span>
    </div>
    <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
      <nav aria-label="Agents on this ticket" className="hidden w-[230px] shrink-0 flex-col overflow-y-auto border-r border-border bg-sidebar md:flex">
        <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-1.5"><h2 className="text-xs font-semibold text-text-muted">Agents on this ticket</h2><LiveDelegate ws={ws} ticket={ticket} /></div>
        <AgentTree snap={snap} />
      </nav>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        {job ? <JobView job={job} snap={snap} /> : <AgentView key={shownAgent.id} agent={shownAgent} snap={snap} ticket={ticket} />}
      </div>
      {side}
    </div>
    <Sheet open={drawer} onOpenChange={setDrawer}>
      <SheetContent side="left" className="w-[320px] max-w-[88vw] gap-0 p-0" aria-describedby={undefined}>
        <SheetHeader className="border-b border-border p-4"><SheetTitle className="text-sm">Agents on this ticket</SheetTitle><SheetDescription className="sr-only">Pick an agent or job to look at.</SheetDescription></SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto"><LiveProvider value={{ ...ctx, select: (next) => { setSelected(next); setDrawer(false) } }}><AgentTree snap={snap} /></LiveProvider></div>
      </SheetContent>
    </Sheet>
    <RequestSheet request={snap.requests[0]} open={request} onClose={() => setRequest(false)} />
  </LiveProvider>
}
