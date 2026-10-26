import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { queries } from '@/api/queries'
import { cleanupDoubt, exitText, runQueries, runsApi, type Run } from '@/api/runs'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

const limitedText = 'Delegation visibility is limited in this version'
type Task = { id: string; title: string; state: string; origin: string; harness: string; model?: string | null; result?: string | null; run?: Run }
const active = (run: Run) => run.state === 'running' || run.state === 'starting'

function useTasks(ticket?: string) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  const real = isRealHost()
  const runs = useQuery({ ...runQueries.list(ws), enabled: !!ws && !!ticket && real, retry: false, refetchInterval: 5000 })
  const sessions = useQuery({ ...queries.agents(ws), enabled: !!ws && !!ticket, retry: false, refetchInterval: 5000 })
  const ticketRuns = (runs.data?.runs ?? []).filter(r => r.ticket === ticket)
  const main = ticketRuns.filter(r => r.kind === 'agent' && r.state === 'running').sort((a, b) => b.started_at.localeCompare(a.started_at))[0]
  const tasks: Task[] = ticketRuns.filter(r => r !== main).map((run, i) => ({
    id: run.run, title: run.label_hidden ? `Task ${i + 1}` : run.label || `Task ${i + 1}`,
    state: active(run) ? run.grant_state === 'ended' ? 'needs you' : 'running' : run.state === 'lost' || (run.exit?.code != null && run.exit.code !== 0) || run.exit?.reason === 'spawn_failed' ? 'failed' : 'done',
    // Registry runs are dashboard launches. Session-only children come from the agent tree.
    origin: 'Requested by you', harness: run.harness, model: run.model, result: exitText(run), run,
  }))
  const allSessions = sessions.data ?? []
  const belongs = (id: string): boolean => {
    const session = allSessions.find(s => s.session === id)
    return !!session && (session.claims.some(c => c.ticket === ticket) || session.leases.some(l => l.ticket === ticket) || session.waiting_on?.ticket === ticket)
  }
  for (const s of allSessions) {
    if (!/\.\d+$/.test(s.session) || ticketRuns.some(r => r.run === s.session)) continue
    if (!belongs(s.session) && !belongs(s.parent ?? s.session.replace(/\.\d+$/, ''))) continue
    tasks.push({ id: s.session, title: `Task ${tasks.length + 1}`, state: s.state === 'stopped' ? 'done' : s.waiting_on ? 'needs you' : s.state === 'waiting' || s.state === 'idle' ? 'waiting on parent' : 'running', origin: 'Spawned by main agent', harness: s.harness, model: s.model })
  }
  return { ws, tasks, main, running: ticketRuns.filter(active), loading: !!ticket && (sessions.isPending || (real && runs.isPending)), limited: !ticket || sessions.isError || (real && (runs.isError || !!runs.data?.truncated)), unsupported: tasks.some(t => !t.run && t.state !== 'done') }
}

function useStop(ws: string) {
  const client = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  async function stop(runs: Run[]) {
    setBusy(true)
    setMessage('')
    const results = await Promise.allSettled(runs.map(r => runsApi.stop(ws, r.run)))
    if (results.some(r => r.status === 'rejected')) setMessage('Some agents could not be stopped. Try again.')
    else if (results.some(r => r.status === 'fulfilled' && cleanupDoubt(r.value.cleanup))) setMessage('Some processes may still be running.')
    await client.invalidateQueries({ queryKey: runQueries.list(ws).queryKey })
    setBusy(false)
  }
  return { stop, busy, message }
}

export function TasksChip({ ticket }: { ticket?: string }) {
  const data = useTasks(ticket)
  if (data.loading) return <span className="text-xs text-text-muted">Loading tasks...</span>
  if (data.limited) return <span data-testid="focus-tasks-chip" className="text-xs text-text-muted">{limitedText}</span>
  if (!data.tasks.length) return null
  return <Popover><PopoverTrigger asChild><Button data-testid="focus-tasks-chip" variant="outline" size="sm" className="rounded-full">{data.tasks.length} tasks · {data.tasks.filter(t => t.state === 'needs you').length} need you</Button></PopoverTrigger><PopoverContent className="max-h-[70vh] w-[380px] max-w-[90vw] overflow-auto p-0"><TasksList ticket={ticket} /></PopoverContent></Popover>
}

export function TasksList({ ticket }: { ticket?: string }) {
  const data = useTasks(ticket)
  const action = useStop(data.ws)
  return <section data-testid="focus-tasks-list" aria-label="Tasks" className="border-t border-border p-4 text-sm">
    <h2 className="mb-3 font-medium">Tasks</h2>
    {data.loading ? <p className="text-text-muted">Loading tasks...</p> : <>
      {data.limited && <p className="text-text-muted">{limitedText}</p>}
      {!data.limited && !data.tasks.length && <p className="text-text-muted">No tasks yet.</p>}
      <div className={data.tasks.length ? 'space-y-2 border-l-2 border-border pl-3' : ''}>{data.tasks.map(task => <details key={task.id} className="rounded-lg border border-border bg-surface text-xs [overflow-wrap:anywhere]">
        <summary className="cursor-pointer p-3 focus-visible:outline-2 focus-visible:outline-brand"><strong className="text-[13px]">{task.title}</strong><span className="ml-2 text-text-muted">{task.state}</span><small className="ml-3 mt-1 block text-text-muted">{task.origin}</small></summary>
        <div className="space-y-3 border-t border-border p-3 text-text-muted">
          <p>Result: {task.result || task.state}</p>
          <p>{task.harness} <span className="rounded border border-border px-2 py-0.5">{task.model || 'Model not reported'}</span></p>
          {task.run && <p>Started {new Date(task.run.started_at).toLocaleString()}</p>}
          <Button variant="outline" size="sm" disabled={action.busy || !task.run || !active(task.run)} onClick={() => { if (task.run && window.confirm('Stop this task?')) void action.stop([task.run]) }}>Stop</Button>
          {!task.run && task.state !== 'done' && <p>Stopping this task is not supported by this host.</p>}
        </div>
      </details>)}</div>
    </>}
    {action.message && <p role="alert" className="mt-2 text-xs">{action.message}</p>}
  </section>
}

export function PauseButton({ ticket }: { ticket?: string }) {
  const data = useTasks(ticket)
  const action = useStop(data.ws)
  const count = data.running.filter(r => r !== data.main).length
  return <div data-testid="focus-pause">
    <Button variant="outline" size="sm" disabled={data.loading || data.limited || data.unsupported || !data.running.length || action.busy} title={data.unsupported ? 'Stopping session-only tasks is not supported by this host.' : 'Stop the main agent and all running tasks.'} onClick={() => { if (window.confirm(data.main ? `Stop the main agent and ${count} tasks?` : `Stop ${count} tasks?`)) void action.stop(data.running) }}>{action.busy ? 'Stopping...' : 'Stop all'}</Button>
    {action.message && <p role="alert" className="text-xs">{action.message}</p>}
  </div>
}
