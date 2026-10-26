import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { focusApi, type ResearchSource, type ResearchSources, type SourceExtract } from '@/api/focus'
import { runQueries } from '@/api/runs'
import mockSources from '@/mocks/addons/focus/sources.json'
import mockExtract from '@/mocks/addons/focus/source-extract.json'
import { useWorkspace } from '@/app/workspace'
import { Empty } from './data'

const safe = (url?: string | null) => !!url && /^https?:\/\//i.test(url)
const time = (ts: string | null) => (ts ? new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '')

export function useSources(ticket?: string) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  const real = isRealHost()
  const runs = useQuery({ ...runQueries.list(ws), enabled: !!ws && !!ticket && real, retry: false, refetchInterval: 5000 })
  const mine = (runs.data?.runs ?? []).filter((r) => r.ticket === ticket && r.kind === 'agent' && r.harness !== 'shell' && !r.parent_run)
  const run = [...mine].sort((a, b) => (a.state === 'running' ? 0 : 1) - (b.state === 'running' ? 0 : 1) || b.started_at.localeCompare(a.started_at))[0]
  const live = run?.state === 'running'
  const sources = useQuery({
    queryKey: ['focus', 'sources', ws, run?.run ?? null],
    queryFn: () => (real ? focusApi.sources(ws, run!.run) : Promise.resolve(mockSources as ResearchSources)),
    enabled: !!ws && !!ticket && (real ? !!run : true), retry: false, refetchInterval: live ? 5000 : false,
  })
  return { ws, run: run?.run ?? (real ? undefined : mockSources.run), sources, real }
}

function Extract({ ws, run, id, real }: { ws: string; run: string; id: string; real: boolean }) {
  const q = useQuery({
    queryKey: ['focus', 'source', ws, run, id], retry: false,
    queryFn: () => (real ? focusApi.source(ws, run, id) : Promise.resolve(mockExtract as SourceExtract)),
  })
  if (q.isError) return <p className="py-2 text-sm text-text-muted">Could not load this text.</p>
  if (!q.data) return <p className="py-2 text-sm text-text-muted">Loading...</p>
  return <div className="space-y-2 py-2">
    <p className="text-xs text-text-muted">What the agent got (a summary by the tool, not the page itself)</p>
    <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-2 p-3 text-xs">{q.data.extract || 'No text was kept.'}{q.data.truncated ? '\n...' : ''}</pre>
    {safe(q.data.url) && <a href={q.data.url!} target="_blank" rel="noopener noreferrer" className="text-sm text-brand underline">Open in browser</a>}
  </div>
}

function Row({ s, open, toggle, ws, run, real }: { s: ResearchSource; open: boolean; toggle: () => void; ws: string; run: string; real: boolean }) {
  if (s.kind === 'file') return <li className="min-w-0 border-b border-border p-2 text-sm"><span className="break-all font-mono">{s.path}</span> <span className="text-xs text-text-muted">file · {time(s.read_at)}</span></li>
  if (s.kind === 'search') return <li className="min-w-0 space-y-1 border-b border-border p-2 text-sm">
    <p><span className="break-words">Searched: {s.query}</span> <span className="text-xs text-text-muted">{time(s.read_at)}</span></p>
    {s.results?.length ? <ul className="list-disc pl-5 text-xs text-text-muted">{s.results.map((r) => <li key={r.url} className="break-all">{r.title ? `${r.title} - ` : ''}{r.url}</li>)}</ul> : null}
  </li>
  return <li className="min-w-0 border-b border-border pb-2">
    <button type="button" aria-expanded={open} disabled={!s.has_extract} onClick={toggle} className="flex w-full items-center justify-between gap-2 rounded p-2 text-left text-sm hover:bg-surface-2 disabled:cursor-default disabled:hover:bg-transparent">
      <span className="min-w-0"><span className="block break-words">{s.title || s.host || s.url}</span><span className="block break-all text-xs text-text-muted">{s.url}</span></span>
      <span className="shrink-0 text-xs text-text-muted">{s.has_extract ? time(s.read_at) : 'no text kept'}</span>
    </button>
    {open && <section aria-label={`Text of ${s.host ?? s.url}`}><Extract ws={ws} run={run} id={s.id} real={real} /></section>}
  </li>
}

export function Research({ ticket }: { ticket?: string }) {
  const { ws, run, sources, real } = useSources(ticket)
  const [selected, select] = useState<string | null>(null)
  if (!ticket) return <Empty>No ticket yet. Research will appear here.</Empty>
  if (real && !run) return <Empty>No agent session yet.</Empty>
  if (sources.isError) return <Empty>Could not load research.</Empty>
  if (!sources.data) return <Empty>Loading research…</Empty>
  if (!sources.data.available) return <Empty>{sources.data.reason === 'transcript not found yet' ? 'Waiting for the first message...' : sources.data.reason ?? 'No research is available for this session.'}</Empty>
  if (!sources.data.sources.length) return <Empty>The agent has not read anything yet.</Empty>
  return <ul data-testid="focus-panel-research" aria-label="Sources the agent read" className="space-y-1">
    {sources.data.sources.map((s) => <Row key={s.id} s={s} open={selected === s.id} toggle={() => select(selected === s.id ? null : s.id)} ws={ws} run={run!} real={real} />)}
  </ul>
}
