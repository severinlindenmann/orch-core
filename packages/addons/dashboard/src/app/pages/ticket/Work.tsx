import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { toast } from 'sonner'
import { hasFeature } from '@/api/features'
import { liveQueries, type WorkRow, type WorkSession } from '@/api/live'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { thumb } from '@/mocks/addons/focus/live'
import { MilestoneGlyph, PreviewTag, StateDot } from '../focus/live/parts'
import { Pill, type TabProps } from './shared'

type Filter = 'all' | 'jobs' | 'you' | 'bad'
const FILTERS: { id: Filter; label: string }[] = [{ id: 'all', label: 'Everything' }, { id: 'jobs', label: 'Jobs and results' }, { id: 'you', label: 'Your decisions' }, { id: 'bad', label: 'Problems' }]
const keep = (r: WorkRow, f: Filter) => f === 'all' || (f === 'jobs' && r.tags.includes('job')) || (f === 'you' && r.tags.includes('you')) || (f === 'bad' && r.tags.includes('bad'))

export function useWorkLog(ticket: string) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  return useQuery({ ...liveQueries.work(ws, ticket), enabled: !!ws })
}

function Thumbs({ n }: { n: number }) {
  return <span className="mt-1.5 flex flex-wrap gap-1.5">{Array.from({ length: Math.min(n, 6) }, (_, i) => <img key={i} alt="" src={thumb(i, true)} className="size-11 rounded-md border border-border" />)}{n > 6 && <span className="grid size-11 place-items-center rounded-md border border-border text-[11px] text-text-muted">+{n - 6}</span>}</span>
}

function Row({ r }: { r: WorkRow }) {
  const you = r.tags.includes('you')
  return <div data-testid="work-row" data-tags={r.tags.join(' ')} className={cn('grid grid-cols-[44px_18px_minmax(0,1fr)_auto] items-baseline gap-2 border-b border-border py-2 text-[13px] last:border-b-0', you && 'rounded-md border-b-0 bg-brand-soft px-1.5')}>
    <span className="font-mono text-[11px] text-text-faint">{r.time}</span>
    {r.icon === 'job' ? <span aria-hidden className="text-text-faint">▤</span> : r.icon === 'you' ? <span aria-hidden className="text-brand">●</span> : <MilestoneGlyph icon={r.icon} className="translate-y-0.5 text-text-muted" />}
    <span className="min-w-0 [overflow-wrap:anywhere]"><b className="font-semibold">{r.lead}</b> {r.text}
      {r.status && <span className={cn('ml-1.5 text-xs', r.status.tone === 'ok' ? 'text-success' : 'text-danger')}>{r.status.tone === 'ok' ? '✓' : '✗'} {r.status.text}</span>}
      {r.link && <a href="#open" onClick={(e) => { e.preventDefault(); toast('Preview: opens the tool\'s own page or the file.') }} className="ml-2 text-xs text-brand hover:underline">{r.link} ↗</a>}
      {!!r.thumbs && <Thumbs n={r.thumbs} />}
      {r.table && <table className="mt-1.5 w-full text-xs"><tbody>{r.table.map((t, i) => <tr key={i}>{t.map((c, j) => <td key={j} className="border-b border-border px-1.5 py-1">{c}</td>)}</tr>)}</tbody></table>}
    </span>
    <span className="font-mono text-[11px] text-text-faint">{r.right}</span>
  </div>
}

function Session({ s, open, filter, ticket, focusOn }: { s: WorkSession; open: boolean; filter: Filter; ticket: string; focusOn: boolean }) {
  const rows = s.rows.filter((r) => keep(r, filter))
  if (filter !== 'all' && !rows.length) return null
  const bad = s.rows.some((r) => r.tags.includes('bad'))
  return <details data-testid="work-session" data-live={s.live || undefined} open={open || filter !== 'all' || undefined} className={cn('mb-2.5 overflow-hidden rounded-lg border bg-surface [&[open]_.chev]:rotate-90', s.live ? 'border-brand/50' : 'border-border')}>
    <summary className="grid cursor-pointer list-none grid-cols-[12px_minmax(0,1fr)_auto] items-start gap-2.5 px-3.5 py-3 [&::-webkit-details-marker]:hidden">
      <StateDot state={s.live ? 'working' : bad ? 'failed' : 'done'} className="mt-1.5" />
      <span className="min-w-0"><span className="block font-mono text-[11.5px] text-text-faint">{s.when}{s.live && <> · <span className="text-brand">working now</span></>}</span>
        <span className="my-0.5 block font-medium">{s.what}</span>
        <span className="mt-1.5 flex flex-wrap gap-1.5"><Pill tone={s.tool === 'codex' ? 'info' : 'neutral'}>{s.account}</Pill>{s.chips.map((c) => <Pill key={c}>{c}</Pill>)}</span></span>
      <span aria-hidden className="chev text-xs text-text-faint transition-transform">▶</span>
    </summary>
    <div className="border-t border-border px-3.5 pb-3 pt-1.5">
      {rows.map((r, i) => <Row key={i} r={r} />)}
      <div className="mt-2.5 flex flex-wrap gap-2">{s.actions.map((a, j) => a === 'Open focus'
        ? (focusOn ? <Button key={a} size="sm" asChild={focusOn}><Link to="/focus/$ticket" params={{ ticket }}>Open focus</Link></Button> : null)
        : <Button key={a} variant="outline" size="sm" onClick={() => toast(`Preview: "${a}" opens here.`)} className={cn(j === 0 && s.live && 'font-semibold')}>{a}</Button>)}</div>
    </div>
  </details>
}

/** The readable story of a ticket: every agent session, helper and job that worked on it, kept after the run ends. Display only. */
export function Work({ ticket }: TabProps) {
  const { workspace } = useWorkspace()
  const q = useWorkLog(ticket.key)
  const [filter, setFilter] = useState<Filter>('all')
  const log = q.data
  if (q.isPending) return <p className="text-sm text-text-muted" role="status">Loading work...</p>
  if (!log || !log.sessions.length) return <div data-testid="work-empty" className="rounded-lg border border-border bg-surface p-6 text-center text-sm text-text-muted"><p className="font-medium text-text">No agent work recorded on this ticket yet.</p><p className="mt-1">When agents work on it, their sessions, jobs and results show up here.</p></div>
  const all = log.sessions.flatMap((s) => s.rows)
  const files = log.sessions.flatMap((s) => s.chips).reduce((n, c) => n + (/^(\d+) files?$/.exec(c)?.[1] ? Number(/^(\d+)/.exec(c)![1]) : 0), 0)
  const jobs = all.filter((r) => r.icon === 'job').length
  const failed = all.filter((r) => r.tags.includes('bad')).length
  const focusOn = !!workspace && hasFeature('focus', workspace.id)
  let lastDay = ''
  let firstDone = true
  return <div className="space-y-3" data-testid="work-tab">
    <div className="flex items-center gap-2"><h2 className="text-[15px] font-semibold">Work</h2><PreviewTag /></div>
    <div data-testid="work-summary" className="flex flex-wrap gap-x-4 gap-y-1 rounded-lg border border-border bg-surface px-3 py-2.5 text-[13px] text-text-muted">
      <span><b className="text-text">{log.sessions.length} sessions</b> over {log.days} days</span>
      <span><b className="text-text">{jobs} jobs</b>{failed > 0 && ` · ${failed} failed`}</span>
      <span><b className="text-text">{files} files</b> changed</span>
      <span>You decided <b className="text-text">{all.filter((r) => r.tags.includes('you')).length}</b> times</span>
      <span>Estimated API charges <b className="text-text">${log.usd.toFixed(2)}</b> · rest covered by subscription</span>
    </div>
    <div role="group" aria-label="Filter work" className="flex flex-wrap gap-1.5">{FILTERS.map((f) => <button key={f.id} type="button" aria-pressed={filter === f.id} onClick={() => setFilter(f.id)}
      className={cn('rounded-full border px-2.5 py-1 text-xs', filter === f.id ? 'border-brand text-brand' : 'border-border text-text-muted hover:text-text')}>{f.label}</button>)}</div>
    <div>{log.sessions.map((s) => {
      const header = s.day !== lastDay
      lastDay = s.day
      const open = s.live || (!s.live && firstDone)
      if (!s.live && firstDone) firstDone = false
      return <div key={s.id}>
        {header && <div className="mb-2 mt-4 font-mono text-[10.5px] font-semibold uppercase tracking-widest text-text-faint">{s.day}</div>}
        <Session s={s} open={open} filter={filter} ticket={ticket.key} focusOn={focusOn} />
      </div>
    })}</div>
  </div>
}

/** The short version on Overview: the last session, its job results, and the way to the live view or the whole log. */
export function LastWorkCard({ ticket, onSeeAll }: { ticket: string; onSeeAll: () => void }) {
  const { workspace } = useWorkspace()
  const q = useWorkLog(ticket)
  const s = q.data?.sessions[0]
  if (!s) return null
  const focusOn = !!workspace && hasFeature('focus', workspace.id)
  const jobs = s.rows.filter((r) => r.icon === 'job')
  const thumbs = jobs.find((r) => r.thumbs)?.thumbs ?? 0
  return <section data-testid="last-work" aria-label="Last work" className="rounded-lg border border-border bg-surface p-3.5">
    <h3 className="mb-1.5 flex items-center gap-2 text-[13px] font-semibold"><StateDot state={s.live ? 'working' : 'done'} />Last work · {s.live ? 'now' : s.when}<PreviewTag className="ml-auto" /></h3>
    <p className="text-[13px] text-text-muted">{s.what}.</p>
    <ul className="my-2 space-y-1 text-[13px] text-text-muted">{jobs.map((r, i) => <li key={i}>{r.status?.tone === 'bad' ? <span className="text-danger">✗</span> : <span className="text-success">✓</span>} {r.lead.replace(/^Job /, '')}{r.status?.tone === 'bad' ? ` · ${r.status.text}` : ''}</li>)}</ul>
    {thumbs > 0 && <span className="mb-2 flex gap-1.5">{Array.from({ length: 5 }, (_, i) => <img key={i} alt="" src={thumb(i, true)} className="size-9 rounded-md border border-border" />)}<span className="grid size-9 place-items-center rounded-md border border-border text-[11px] text-text-muted">+{thumbs - 5}</span></span>}
    <div className="flex flex-wrap gap-2">
      {focusOn && s.live && <Button size="sm" asChild><Link to="/focus/$ticket" params={{ ticket }}>Open focus</Link></Button>}
      <Button variant="outline" size="sm" onClick={onSeeAll}>See all work</Button>
    </div>
  </section>
}
