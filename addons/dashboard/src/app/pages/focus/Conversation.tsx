import { useId, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
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

export function Conversation({ ticket }: { ticket?: string }) {
  const { workspace } = useWorkspace()
  return workspace ? <ConversationPane key={`${workspace.id}:${ticket ?? ''}`} ws={workspace.id} ticket={ticket} /> : null
}

function ConversationPane({ ws, ticket }: { ws: string; ticket?: string }) {
  const client = useQueryClient()
  const hint = useId()
  const [starting, setStarting] = useState(false)
  const [scratch, setScratch] = useState(() => scratchRuns(ws))
  const [mode, setMode] = useState<'terminal' | 'chat'>('terminal')
  const [lease, setLease] = useState<LeaseState>('reading')
  const terminal = useRef<RealTerminalHandle>(null)
  // The mock host has no PTY API. Its empty state must not fetch a live host.
  const runs = useQuery({ ...runQueries.list(ws), enabled: isRealHost(), retry: false, refetchInterval: 5000 })
  const matching = runs.data?.runs.filter((r) => r.kind === 'agent' &&
    (ticket ? r.ticket === ticket : !r.ticket && scratch.includes(r.run))) ?? []
  const run = matching.filter((r) => r.state === 'running')
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
  return <section data-testid="focus-conversation" aria-label={ticket ? 'Ticket conversation' : 'Scratch conversation'} className="flex min-h-0 min-w-0 flex-1 flex-col">
    <header className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-4 py-3">
      <div role="group" aria-label="Conversation view" className="inline-flex rounded-lg border border-border bg-bg p-1">
        <Button size="sm" variant={chat ? 'ghost' : 'secondary'} aria-pressed={!chat} onClick={() => setMode('terminal')}>Terminal</Button>
        <Button size="sm" variant={chat ? 'secondary' : 'ghost'} aria-pressed={chat} aria-disabled={!run || undefined} aria-describedby={run ? undefined : hint}
          className={run ? undefined : 'cursor-not-allowed text-text-faint'} onClick={() => { if (run) setMode('chat') }}>Chat</Button>
      </div>
      {!run && <p id={hint} className="text-xs text-text-muted">Chat opens once an agent is running</p>}
    </header>
    <div className="relative flex min-h-[280px] min-w-0 flex-1 flex-col overflow-hidden">
      {run ? <>
        <div className={`absolute inset-0 ${chat ? 'invisible' : ''}`} aria-hidden={chat || undefined}><RealTerminal key={run.run} ws={ws} run={run} initial="read" handle={terminal} onLease={setLease} /></div>
        {chat && <Chat ws={ws} run={run} canSend={lease === 'writing'} onSend={(text) => terminal.current?.sendInput(text) ?? false} onTake={() => terminal.current?.take()} />}
      </> :
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-6 text-center text-sm text-text-muted">
          {launching ? <p role="status">Starting agent...</p> : runs.isLoading ? <p role="status">Loading agent session...</p> : runs.isError ? <><p role="alert">Could not load agent sessions.</p><Button variant="outline" onClick={() => void runs.refetch()}>Try again</Button></> : <><p>No agent is running in this conversation.</p><Button onClick={() => setStarting(true)}>Start an agent</Button></>}
        </div>}
    </div>
    {ticket && <NeedsYou ws={ws} ticketKey={ticket} />}
    <StartAgentDialog ws={ws} ticket={ticket} open={starting} choose onClose={() => setStarting(false)} onStarted={started} />
  </section>
}
