import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { toast } from 'sonner'
import { liveApi, liveQueries, type LiveAction, type LiveAgent, type LiveJob, type LiveSnapshot } from '@/api/live'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { Conversation } from '../Conversation'
import { Badge, MilestoneGlyph, Seg, StateDot, TOOL_TONE } from './parts'
import { agentById, cacheView, costKind, fmtTokens, jobProgressPct, mmss, secondsSince, useLive } from './useLive'

type View = 'progress' | 'chat' | 'terminal'

function useAct() {
  const live = useLive()!
  const client = useQueryClient()
  return async (action: LiveAction) => {
    await liveApi.act(live.ws, live.ticket, action)
    await client.invalidateQueries({ queryKey: liveQueries.snapshotKey(live.ws, live.ticket) })
  }
}

export function UsageCard({ agent, snap }: { agent: LiveAgent; snap: LiveSnapshot }) {
  const { now } = useLive()!
  const account = snap.accounts.find((x) => x.key === agent.accountKey)
  const cache = cacheView(agent.cache, now)
  const kind = costKind(agent)
  const usd = `$${agent.usd.toFixed(2)}`
  const cell = (k: string, main: string, sub: string, extra?: React.ReactNode) => <div className="min-w-0"><span className="block text-[10.5px] uppercase tracking-wide text-text-faint">{k}</span><b className="block text-[13px] font-semibold text-text">{main}</b><span className="block text-xs text-text-muted">{sub}</span>{extra}</div>
  return <div data-testid="live-usage" className="grid grid-cols-2 gap-x-4 gap-y-3 border-b border-border bg-surface px-4 py-3 lg:grid-cols-3">
    {cell('Model', agent.model, agent.account)}
    {kind === 'local' ? cell('Cost', 'Runs on your server', 'No API charge; server costs not counted')
      : kind === 'subscription' ? cell('Cost', 'Covered by subscription', `API-price equivalent ≈ ${usd} · not an extra charge`)
      : cell('Cost', `Estimated API charges ${usd}`, 'estimate, not a bill')}
    {account ? cell('Account allowance', `${account.pct}% used`, `${account.reset} · shared by all agents on this account`) : cell('Account allowance', '—', 'no allowance')}
    {cache ? cell('Cache', `${cache.kind === 'warm' ? '●' : cache.kind === 'cooling' ? '◐' : '○'} ${cache.text}`, cache.sub) : cell('Cache', '—', agent.tool === 'local' ? 'local model, no provider cache' : 'not started')}
    {cell('Conversation space', `${agent.space}% used`, '', <span className="mt-1 block h-1.5 overflow-hidden rounded-full bg-surface-3"><i className="block h-full bg-brand" style={{ width: `${agent.space}%` }} /></span>)}
    {cell('Tokens', fmtTokens(agent.tokens.read + agent.tokens.written), `${fmtTokens(agent.tokens.read)} read (${fmtTokens(agent.tokens.reused)} reused) · ${fmtTokens(agent.tokens.written)} written`)}
  </div>
}

function Progress({ agent }: { agent: LiveAgent }) {
  const { now } = useLive()!
  return <ol aria-label="Progress" className="divide-y divide-border">
    {agent.milestones.map((m) => {
      const s = secondsSince(agent.since, now)
      const right = m.tone === 'live' ? <span className="text-brand">● {s ?? ''}{s != null && ' s'}</span> : m.right
      const body = <>
        <span className="font-mono text-[11px] text-text-faint">{m.at}</span>
        <MilestoneGlyph icon={m.icon} className={cn('mt-0.5', m.tone === 'ok' && 'text-success', m.tone === 'warn' && 'text-warning', m.tone === 'live' && 'text-brand', m.tone === 'past' && 'text-text-faint')} />
        <span className={cn('min-w-0 text-[13px]', m.tone === 'past' ? 'text-text-muted' : 'text-text')}>{m.text}{m.command && <span className="ml-1 text-xs text-text-faint">▸ details</span>}</span>
        <span className={cn('font-mono text-[11px] text-text-faint', m.tone === 'warn' && 'text-warning')}>{right}</span>
      </>
      const grid = 'grid grid-cols-[44px_16px_minmax(0,1fr)_auto] items-start gap-2 px-4 py-2'
      return m.command
        ? <li key={m.id}><details data-testid="live-milestone" className="group"><summary className={cn(grid, 'cursor-pointer list-none hover:bg-surface-2 [&::-webkit-details-marker]:hidden')}>{body}</summary>
          <div className="space-y-1.5 bg-surface-2 px-4 pb-3 pl-[76px]"><code className="block font-mono text-xs text-text-muted">{m.command}</code>{m.output && <pre className="overflow-auto rounded-md bg-bg p-2 font-mono text-xs leading-5 text-text-muted">{m.output}</pre>}</div></details></li>
        : <li key={m.id} className={grid}>{body}</li>
    })}
  </ol>
}

function Plain({ agent, view }: { agent: LiveAgent; view: 'chat' | 'terminal' }) {
  if (view === 'terminal') return <pre aria-label="Terminal of this agent" className="h-full min-h-40 overflow-auto bg-black p-4 font-mono text-xs leading-5 text-text-muted">{agent.terminal}</pre>
  return <div role="log" aria-label="Chat of this agent" className="space-y-2 p-4">
    {agent.chat.map((c, i) => <p key={i} className={cn('max-w-[85%] rounded-lg px-3 py-2 text-[13px]', c.from === 'you' ? 'ml-auto bg-brand-soft' : 'bg-surface-2')}>{c.text}</p>)}
    <p className="text-xs text-text-faint">Same chat view as the main agent, one per agent.</p>
  </div>
}

export function AgentView({ agent, snap, ticket }: { agent: LiveAgent; snap: LiveSnapshot; ticket: string }) {
  const live = useLive()!
  const [view, setView] = useState<View>('progress')
  const kids = snap.agents.filter((a) => a.parent === agent.id).length
  const stoppable = agent.kind !== 'main' && agent.state !== 'done'
  return <section data-testid="live-agent" aria-label={agent.name} className="flex min-h-0 min-w-0 flex-1 flex-col">
    <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-border px-4 py-2">
      <StateDot state={agent.state} />
      <h2 className="min-w-0 truncate text-sm font-semibold">{agent.name}</h2>
      {live.details && <Badge className={TOOL_TONE[agent.tool]}>{agent.model} · {agent.account.split(' · ')[0]}</Badge>}
      <span className="flex-1" />
      {stoppable && <Button variant="outline" size="sm" title={`${kids ? `Also stops its ${kids} helper. ` : ''}The main agent keeps working.`} onClick={() => toast(`${agent.name} stopped (preview, nothing really stopped).`)}>{agent.kind === 'helper' ? 'Stop this helper' : 'Stop this task'}</Button>}
      <Seg label="Agent view" value={view} onChange={setView} options={[{ id: 'progress', label: 'Progress' }, { id: 'chat', label: 'Chat' }, { id: 'terminal', label: 'Terminal' }]} />
    </header>
    {live.details && <UsageCard agent={agent} snap={snap} />}
    <div className="relative min-h-0 flex-1 overflow-auto">
      {view === 'progress' && <Progress agent={agent} />}
      {view !== 'progress' && (agent.kind === 'main' ? <Conversation ticket={ticket} forcedMode={view} /> : <Plain agent={agent} view={view} />)}
    </div>
  </section>
}

const STEP_ICON = { ok: <span className="text-success">✓</span>, running: <span className="text-brand">●</span>, queued: <span className="text-text-faint">○</span>, failed: <span className="text-danger">✗</span> }

export function JobView({ job, snap }: { job: LiveJob; snap: LiveSnapshot }) {
  const live = useLive()!
  const act = useAct()
  const parent = agentById(snap, job.parent)
  const s = job.state
  const secs = secondsSince(job.since, live.now)
  const events = snap.events.filter((e) => {
    const id = String(e.job ?? '')
    return job.id === 'j1' ? id === 'dbx-551' : job.id === 'j2' ? id === 'img-7' : id === 'dbt-3'
  })
  return <section data-testid="live-job" aria-label={job.name} className="min-h-0 min-w-0 flex-1 overflow-auto">
    <header className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2">
      <h2 className="min-w-0 truncate text-sm font-semibold">{job.name}</h2><Badge>{job.source}</Badge><span className="flex-1" />
      {job.link && <Button asChild variant="outline" size="sm"><a href={job.link.url} target="_blank" rel="noreferrer noopener" onClick={(e) => { e.preventDefault(); toast('Preview: this link opens the tool\'s own page.') }}>{job.link.label} <ExternalLink aria-hidden /></a></Button>}
    </header>
    <div className="space-y-3 border-b border-border px-4 py-3">
      <div className={cn('text-sm font-semibold', s === 'failed' ? 'text-danger' : s === 'working' ? 'text-brand' : 'text-success')}>
        {s === 'working' ? '● Running' : s === 'failed' ? '✗ Failed' : '✓ Done'}{s === 'working' && secs != null && <span className="ml-2 font-mono text-xs font-normal text-text-faint">{mmss(secs)}</span>}
      </div>
      <p className="text-xs text-text-muted">Started by <b className="text-text">{parent?.name ?? 'an agent'}</b> through a {job.via}{live.details && <> · <code className="font-mono">{job.cmd}</code></>}</p>
      {s === 'failed' ? <p className="text-sm text-danger">{job.summary}</p>
        : <div role="progressbar" aria-label="Job progress" aria-valuenow={jobProgressPct(job)} aria-valuemin={0} aria-valuemax={100} className="h-2 overflow-hidden rounded-full bg-surface-3"><i className="block h-full bg-brand transition-all" style={{ width: `${jobProgressPct(job)}%` }} /></div>}
    </div>
    <dl className="grid grid-cols-2 gap-3 border-b border-border px-4 py-3 sm:grid-cols-3">{job.metrics.map((m) => <div key={m.label}><dt className="text-[10.5px] uppercase tracking-wide text-text-faint">{m.label}</dt><dd className="text-[13px] font-semibold">{m.value}</dd></div>)}</dl>
    {job.steps.length > 0 && <div className="border-b border-border px-4 py-3"><h3 className="mb-1 text-xs font-semibold text-text-muted">Steps</h3>
      {job.steps.map((st) => <div key={st.name} data-state={st.state} className="flex items-center gap-2 py-1 text-[13px]">{STEP_ICON[st.state]}<span className="font-mono text-xs">{st.name.replace(/_/g, ' ')}</span><span className="flex-1" /><span className="font-mono text-[11px] text-text-faint">{st.state === 'running' ? 'running' : st.time}</span></div>)}</div>}
    {job.images.length > 0 && <div className="border-b border-border px-4 py-3"><h3 className="mb-2 text-xs font-semibold text-text-muted">Images <span className="font-normal text-text-faint">{job.progress.done} of {job.progress.total}</span></h3>
      <div data-testid="live-images" className="grid grid-cols-3 gap-2 sm:grid-cols-4">{job.images.map((im, i) => <figure key={i} className={cn('m-0', !im.src && 'opacity-50')}>
        {im.src ? <img alt={im.name} src={im.src} className="aspect-square w-full rounded-md border border-border" /> : <div className="aspect-square w-full rounded-md border border-border bg-surface-3" />}
        <figcaption className="truncate text-[11px] text-text-faint">{im.src ? im.name : 'waiting'}</figcaption></figure>)}</div>
      <div className="mt-2 flex gap-2"><Button variant="outline" size="sm" onClick={() => toast('Preview: opens the folder with the images.')}>Open folder</Button></div></div>}
    {job.table && <div className="space-y-2 border-b border-border px-4 py-3"><h3 className="text-xs font-semibold text-text-muted">{job.table.title}</h3>
      <table className="w-full text-left text-xs"><thead><tr>{job.table.cols.map((c) => <th key={c} className="border-b border-border py-1 pr-2 font-medium text-text-faint">{c}</th>)}</tr></thead>
        <tbody>{job.table.rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} className="border-b border-border py-1.5 pr-2 align-top">{c}</td>)}</tr>)}</tbody></table>
      {job.ignored ? <p className="text-xs text-text-muted">You chose to ignore this for this run.</p> : job.fixAsked ? <p className="text-xs text-text-muted">You asked the main agent to fix this.</p> : <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={() => void act({ op: 'ask-fix', job: job.id })}>Ask the main agent to fix</Button>
        <Button variant="outline" size="sm" onClick={() => void act({ op: 'ignore-job', job: job.id })}>Ignore for this run</Button></div>}
      {live.details && job.log && <details><summary className="cursor-pointer text-xs text-text-muted">Log (last lines)</summary><pre className="mt-1 overflow-auto rounded-md bg-bg p-2 font-mono text-xs text-text-muted">{job.log}</pre></details>}
    </div>}
    {live.details && <details data-testid="live-events" className="px-4 py-3"><summary className="cursor-pointer text-xs text-text-muted">Technical details · events (orch.job/1)</summary>
      <div className="mt-2 space-y-1">{events.slice(-12).map((e, i) => <code key={i} className="block break-all font-mono text-[11px] text-text-muted">{JSON.stringify(e)}</code>)}</div></details>}
  </section>
}
