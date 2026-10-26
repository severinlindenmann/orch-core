import { useId, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { focusRunQueries } from '@/api/focus'
import { isDemoRun } from '@/mocks/addons/focus/demo'
import { runQueries, type Run, type RunList } from '@/api/runs'
import { useWorkspace } from '@/app/workspace'
import { RealTerminal, type LeaseState, type RealTerminalHandle } from '@/app/terminal/real/RealTerminal'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { Button } from '@/components/ui/button'
import { Chat } from './conversation/Chat'
import { NeedsYou } from './conversation/NeedsYou'

const scratchKey = (ws: string) => `orch.focus.runs.${ws}`
function scratchRuns(ws: string): string[] {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(scratchKey(ws)) ?? '[]')
    return Array.isArray(value) ? value.filter((id): id is string => typeof id === 'string') : []
  } catch { return [] }
}

export function Conversation({ ticket, scratchRun, onReview }: { ticket?: string; scratchRun?: string; onReview?: () => void }) {
  const { workspace } = useWorkspace()
  return workspace ? <ConversationPane key={`${workspace.id}:${ticket ?? ''}:${scratchRun ?? ''}`} ws={workspace.id} ticket={ticket} scratchRun={scratchRun} onReview={onReview} /> : null
}

function ConversationPane({ ws, ticket, scratchRun, onReview }: { ws: string; ticket?: string; scratchRun?: string; onReview?: () => void }) {
  const client = useQueryClient()
  const hint = useId()
  const [starting, setStarting] = useState(false)
  const [scratch, setScratch] = useState(() => scratchRuns(ws))
  const [mode, setMode] = useState<'terminal' | 'chat'>('terminal')
  const [lease, setLease] = useState<LeaseState>('reading')
  const terminal = useRef<RealTerminalHandle>(null)
  // The mock host has no PTY API: it answers with the demo run of DEMO-0043 and draws a canned terminal.
  const real = isRealHost()
  const runs = useQuery({ ...focusRunQueries.list(ws), retry: false, refetchInterval: real ? 5000 : false })
  // A scratch session opened from "No ticket" is the person's own harness terminal (kind shell, run id in the address).
  const matching = runs.data?.runs.filter((r) => scratchRun && !ticket ? r.run === scratchRun : (r.kind === 'agent' || (r.as_person && r.harness !== 'shell')) &&
    (ticket ? r.ticket === ticket : !r.ticket && scratch.includes(r.run))) ?? []
  const run = matching.filter((r) => r.state === 'running' && !(ticket && r.parent_run))
    .sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))[0]
  const launching = matching.some((r) => r.state === 'starting')
  function started(next: Run) {
    if (!ticket) {
      const ids = [...scratch, next.run]
      setScratch(ids)
      try { sessionStorage.setItem(scratchKey(ws), JSON.stringify(ids)) } catch { /* Keep this session in memory. */ }
    }
    client.setQueryData<RunList>(runQueries.list(ws).queryKey, (old) => ({
      runs: [next, ...(old?.runs ?? []).filter((r) => r.run !== next.run)],
      runner: old?.runner ?? null, truncated: old?.truncated ?? false,
    }))
  }
  const chat = mode === 'chat' && !!run
  const demo = !!run && isDemoRun(run.run)
  return <section data-testid="focus-conversation" aria-label={ticket ? 'Ticket conversation' : 'Scratch conversation'} className="flex min-h-0 min-w-0 flex-1 flex-col">
    <header className="flex shrink-0 flex-nowrap items-center gap-3 border-b border-border px-4 py-2">
      <div role="group" aria-label="Conversation view" className="inline-flex rounded-lg border border-border bg-bg p-1">
        <Button size="sm" variant={chat ? 'ghost' : 'secondary'} aria-pressed={!chat} onClick={() => setMode('terminal')}>Terminal</Button>
        <Button size="sm" variant={chat ? 'secondary' : 'ghost'} aria-pressed={chat} aria-disabled={!run || undefined} aria-describedby={run ? undefined : hint}
          className={run ? undefined : 'cursor-not-allowed text-text-muted'} onClick={() => { if (run) setMode('chat') }}>Chat</Button>
      </div>
    </header>
    <div className="relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
      {run ? <>
        <div className={`absolute inset-0 ${chat ? 'invisible' : ''}`} aria-hidden={chat || undefined}>{!isDemoRun(run.run) ? <RealTerminal key={run.run} ws={ws} run={run} initial={scratchRun && !ticket ? 'write' : 'read'} handle={terminal} onLease={setLease} /> : <DemoTerminal />}</div>
        {chat && <Chat ws={ws} run={run} canSend={demo || lease === 'writing'} onSend={(text) => demo ? true : terminal.current?.sendInput(text) ?? false} onTake={() => terminal.current?.take()} />}
      </> :
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center text-sm text-text-muted">
          {launching ? <p role="status">Starting agent...</p> : runs.isLoading ? <p role="status">Loading agent session...</p> : runs.isError ? <><p role="alert">Could not load agent sessions.</p><Button variant="outline" onClick={() => void runs.refetch()}>Try again</Button></> : <><p id={hint} className="font-medium text-text">No agent running</p><p>Start an agent to use Terminal and Chat.</p><Button onClick={() => setStarting(true)}>Start an agent</Button></>}
        </div>}
    </div>
    {ticket && <NeedsYou ws={ws} ticketKey={ticket} onReview={onReview} />}
    <StartAgentDialog ws={ws} ticket={ticket} open={starting} choose onClose={() => setStarting(false)} onStarted={started} />
  </section>
}

/** The mock host has no PTY: a fixed screen of what the demo agent printed. */
function DemoTerminal() {
  return <pre data-testid="focus-demo-terminal" aria-label="Demo terminal" className="h-full overflow-auto bg-black p-4 font-mono text-xs leading-5 text-text-muted">{`> Load the tariff tables as dbt seeds. Keep the meter join unchanged.

I read the billing models and the two tariff CSVs. I will load
them as seeds and add a not-null test on valid_from.

* Edit seeds/t1_load.csv
  ok

Should valid_from in the seeds be a DATE or a TIMESTAMP?
  1. DATE (local midnight)
  2. TIMESTAMP (UTC)

(waiting for you)`}</pre>
}
