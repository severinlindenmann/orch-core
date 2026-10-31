import { useState } from 'react'
import { FileSpreadsheet } from 'lucide-react'
import type { LiveAgent, LiveJob, LiveSnapshot, LiveState } from '@/api/live'
import { cn } from '@/lib/utils'
import { Badge, StateDot, TOOL_TONE } from './parts'
import { cacheBadgeText, cacheView, jobProgressPct, secondsSince, useLive } from './useLive'

const jobState = (j: LiveJob): LiveState => (j.state === 'failed' ? 'failed' : j.state === 'done' ? 'done' : 'working')

function Timer({ since }: { since: string | null }) {
  const live = useLive()
  const s = secondsSince(since, live?.now ?? Date.now())
  return s == null ? null : <span className="ml-1 font-mono text-[11px] text-text-faint">{s} s</span>
}

function AgentRow({ a }: { a: LiveAgent }) {
  const live = useLive()!
  const selected = live.selected === a.id
  const cache = cacheView(a.cache, live.now)
  return <div data-testid={`live-node-${a.id}`} data-state={a.state} style={{ marginLeft: a.depth * 16 }} className={cn('rounded-md border border-transparent', selected && 'border-border bg-surface-2')}>
    <button type="button" aria-current={selected || undefined} onClick={() => live.select(a.id)} className="flex w-full min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-brand">
      <StateDot state={a.state} className="mt-1.5" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium text-text">{a.name} <span className="font-normal text-text-muted">· {a.stateText}</span></span>
        <span className="block text-xs text-text-muted">{a.now}{a.state === 'working' && <Timer since={a.since} />}</span>
        {live.details && <span className="mt-1 flex flex-wrap gap-1">
          <Badge className={TOOL_TONE[a.tool]}>{a.account.split(' · ')[0]}</Badge><Badge>{a.model}</Badge>{cache && <Badge>{cacheBadgeText(cache)}</Badge>}
        </span>}
      </span>
    </button>
    {a.state === 'blocked' && a.request && <button type="button" onClick={live.openRequest} className="mb-1.5 ml-6 text-xs font-medium text-brand underline-offset-2 hover:underline">Review request</button>}
  </div>
}

function JobRow({ j, depth }: { j: LiveJob; depth: number }) {
  const live = useLive()!
  const selected = live.selected === j.id
  const s = jobState(j)
  return <div data-testid={`live-node-${j.id}`} data-state={s} style={{ marginLeft: depth * 16 }} className={cn('rounded-md border border-transparent', selected && 'border-border bg-surface-2')}>
    <button type="button" aria-current={selected || undefined} onClick={() => live.select(j.id)} className="flex w-full min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-brand">
      <FileSpreadsheet aria-hidden className={cn('mt-0.5 size-3.5 shrink-0', s === 'failed' ? 'text-danger' : s === 'working' ? 'text-brand' : 'text-text-faint')} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium text-text">{j.name} <Badge>{j.source}</Badge></span>
        <span className={cn('block text-xs', s === 'failed' ? 'text-danger' : 'text-text-muted')}>{j.now}</span>
        {s === 'working' && <span role="progressbar" aria-label={`${j.name} progress`} aria-valuenow={jobProgressPct(j)} aria-valuemin={0} aria-valuemax={100} className="mt-1 block h-1 overflow-hidden rounded-full bg-surface-3"><i className="block h-full bg-brand transition-all" style={{ width: `${jobProgressPct(j)}%` }} /></span>}
        {live.details && <span className="mt-1 block"><Badge>via {j.via}</Badge></span>}
      </span>
    </button>
  </div>
}

export function AgentTree({ snap }: { snap: LiveSnapshot }) {
  const live = useLive()!
  const [showDone, setShowDone] = useState(false)
  const visible = (a: LiveAgent) => showDone || a.state !== 'done' || a.id === live.selected
  const groups: { title: string; agents: LiveAgent[] }[] = [
    { title: 'Main agent and helpers', agents: snap.agents.filter((a) => a.kind !== 'task') },
    { title: 'Tasks orch started on other tools', agents: snap.agents.filter((a) => a.kind === 'task') },
  ]
  const hiddenIn = (agents: LiveAgent[]) => agents.filter((a) => !visible(a)).length
  const total = hiddenIn(snap.agents)
  return <div data-testid="live-tree" className="space-y-3 p-2">
    {groups.map((g, gi) => {
      const hidden = hiddenIn(g.agents)
      return <section key={g.title} aria-label={g.title}>
        <div className="mb-1 px-2 text-[11px] font-medium uppercase tracking-wide text-text-faint">{g.title}</div>
        {g.agents.filter(visible).map((a) => <div key={a.id}>
          <AgentRow a={a} />
          {snap.jobs.filter((j) => j.parent === a.id).map((j) => <JobRow key={j.id} j={j} depth={a.depth + 1} />)}
        </div>)}
        {gi === 0 && (hidden > 0 || showDone) && <button type="button" onClick={() => setShowDone(!showDone)} className="ml-2 mt-1 text-xs text-brand hover:underline">{showDone ? 'Hide finished' : `Show ${total} finished`}</button>}
      </section>
    })}
  </div>
}

export const treeSummary = (snap: LiveSnapshot) => `${snap.agents.filter((a) => a.state === 'working').length} working, ${snap.requests.length} needs you`
