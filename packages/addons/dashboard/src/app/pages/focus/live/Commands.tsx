import { useState } from 'react'
import type { LiveCommand } from '@/api/live'
import { cn } from '@/lib/utils'
import { Seg } from './parts'
import { agentById, mmss, secondsSince, useLive } from './useLive'

type Scope = 'sel' | 'all'
type Filter = 'all' | LiveCommand['status']

const ICON: Record<LiveCommand['status'], { glyph: string; cls: string; label: string }> = {
  ok: { glyph: '✓', cls: 'text-success', label: 'Done' },
  failed: { glyph: '✗', cls: 'text-danger', label: 'Failed' },
  running: { glyph: '●', cls: 'text-brand', label: 'Running' },
  waiting: { glyph: '✋', cls: 'text-warning', label: 'Waiting' },
}

/** Commands the agents ran. Secrets are hidden before they get here (shown as ••••). Display only. */
export function Commands() {
  const live = useLive()
  const [scope, setScope] = useState<Scope>('sel')
  const [filter, setFilter] = useState<Filter>('all')
  if (!live) return null
  // A job selected in the tree shows the commands of the agent that started it.
  const job = live.snap.jobs.find((j) => j.id === live.selected)
  const agentId = job ? job.parent : live.selected
  const agent = agentById(live.snap, agentId)
  const list = live.snap.commands.filter((c) => (scope === 'all' || c.agent === agentId) && (filter === 'all' || c.status === filter))
  return <div data-testid="live-commands" className="space-y-3">
    <div className="flex flex-wrap gap-2">
      <Seg label="Whose commands" value={scope} onChange={setScope} options={[{ id: 'sel', label: 'This agent' }, { id: 'all', label: 'All agents on this ticket' }]} />
      <Seg label="Status" value={filter} onChange={setFilter} options={[{ id: 'all', label: 'All' }, { id: 'running', label: 'Running' }, { id: 'failed', label: 'Failed' }, { id: 'waiting', label: 'Waiting' }]} />
    </div>
    {list.length === 0 ? <p className="text-xs text-text-muted">No commands {filter !== 'all' ? 'with this status ' : ''}for {scope === 'sel' ? agent?.name ?? 'this agent' : 'this ticket'}.</p>
      : <ul className="divide-y divide-border rounded-lg border border-border">{list.map((c) => {
        const i = ICON[c.status]
        const by = agentById(live.snap, c.agent)
        const s = secondsSince(c.since, live.now)
        return <li key={c.id}><details data-testid="live-command" data-status={c.status}>
          <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs hover:bg-surface-2 [&::-webkit-details-marker]:hidden">
            <span aria-label={i.label} className={cn('w-4 shrink-0 text-center', i.cls)}>{i.glyph}</span>
            <code className="min-w-0 flex-1 truncate font-mono">{c.command}</code>
            <span className="shrink-0 font-mono text-[11px] text-text-faint">{c.status === 'running' && s != null ? mmss(s) : c.duration}</span>
          </summary>
          <div className="space-y-1 bg-surface-2 px-3 pb-3 pl-9">
            {scope === 'all' && by && <p className="text-[11px] text-text-faint">Run by {by.name}</p>}
            <pre className="overflow-auto rounded-md bg-bg p-2 font-mono text-xs leading-5 text-text-muted">{c.redacted ? '(secret hidden)\n' : ''}{c.output}</pre>
          </div></details></li>
      })}</ul>}
  </div>
}
