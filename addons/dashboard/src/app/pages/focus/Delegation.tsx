import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { focusApi, type DelegationChild, type Delegations } from '@/api/focus'
import mockDelegations from '@/mocks/addons/focus/delegations.json'
import { cleanupDoubt, exitText, runQueries, runsApi, type Run } from '@/api/runs'
import { useWorkspace } from '@/app/workspace'
import { Button } from '@/components/ui/button'
import { DelegateSheet } from './DelegateSheet'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'

const limitedText = 'Delegation visibility is limited in this version'
type Task = { spawnedBy?: DelegationChild['spawned_by']; transcript?: DelegationChild['transcript']; id: string; title: string; state: string; origin: string; harness: string; model?: string | null; result?: string | null; run?: Run; files?: string[]; depth?: number; endedAt?: string | null; startedAt?: string | null }
const active = (run: Run) => run.state === 'running' || run.state === 'starting'
const childState = (c: DelegationChild) => c.state === 'running' ? 'running' : c.state === 'failed' ? 'failed' : 'done'

function useTasks(ticket?: string) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  const real = isRealHost()
  const runs = useQuery({ ...runQueries.list(ws), enabled: !!ws && !!ticket && real, retry: false, refetchInterval: 5000 })
  // Real children come from the host (the runner reads the harness files); the mock host serves a fixture.
  const delegations = useQuery({ queryKey: ['focus', 'delegations', ws, ticket ?? null], queryFn: () => real ? focusApi.delegations(ws, ticket!) : Promise.resolve(mockDelegations as Delegations), enabled: !!ws && !!ticket, retry: false, refetchInterval: 5000 })
  const ticketRuns = (runs.data?.runs ?? []).filter(r => r.ticket === ticket)
  const main = ticketRuns.filter(r => r.kind === 'agent' && r.state === 'running').sort((a, b) => b.started_at.localeCompare(a.started_at))[0]
  const sessions = delegations.data?.sessions ?? []
  const known = new Set<string>()
  const tasks: Task[] = []
  for (const s of sessions) for (const c of s.children) {
    const run = c.run ? ticketRuns.find(r => r.run === c.run) : undefined
    if (c.run) known.add(c.run)
    tasks.push({
      id: c.id, title: c.title, state: run ? (active(run) ? run.grant_state === 'ended' ? 'needs you' : 'running' : childState(c)) : childState(c),
      origin: c.spawned_by === 'agent' ? 'Spawned by main agent' : 'Requested by you', harness: c.harness ?? run?.harness ?? 'agent', model: c.model ?? run?.model,
      spawnedBy: c.spawned_by, transcript: c.transcript,
      result: run ? exitText(run) : childState(c), run, files: c.files, depth: c.depth, startedAt: c.started_at, endedAt: c.ended_at,
    })
  }
  ticketRuns.filter(r => r !== main && !known.has(r.run)).forEach((run, i) => tasks.push({
    id: run.run, title: run.label_hidden ? `Task ${tasks.length + i + 1}` : run.label || `Task ${tasks.length + i + 1}`,
    state: active(run) ? run.grant_state === 'ended' ? 'needs you' : 'running' : run.state === 'lost' || (run.exit?.code != null && run.exit.code !== 0) || run.exit?.reason === 'spawn_failed' ? 'failed' : 'done',
    // Registry runs without a parent link are dashboard launches of the person.
    origin: 'Requested by you', harness: run.harness, model: run.model, result: exitText(run), run,
  }))
  const mainSession = main ? sessions.find(s => s.run === main.run) : undefined
  const hidden = sessions.some(s => !s.visibility.available && s.harness !== 'shell')
  return { ws, tasks, main, budget: { used: mainSession?.children.length ?? 0, max: mainSession?.policy?.max_children ?? null }, running: ticketRuns.filter(active), loading: !!ticket && (delegations.isPending || (real && runs.isPending)), limited: !ticket || delegations.isError || delegations.data?.supported === false || hidden || (real && (runs.isError || !!runs.data?.truncated)), unsupported: tasks.some(t => !t.run && t.state === 'running') }
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

/** P2-6: a child a person delegated becomes a session of its own; one inside the agent's process starts anew. */
function useSeparate(ws: string, ticket?: string) {
  const client = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [note, setNote] = useState<string | null>(null)
  async function refresh() {
    await client.invalidateQueries({ queryKey: ['focus', 'delegations', ws, ticket ?? null] })
    await client.invalidateQueries({ queryKey: runQueries.list(ws).queryKey })
  }
  async function detach(run: string) {
    setBusy(true)
    setMessage('')
    try { await focusApi.detach(ws, run); await refresh() } catch { setMessage('Could not make this task a separate session. Try again.') }
    setBusy(false)
  }
  async function startAnew(task: Task) {
    setBusy(true)
    setMessage('')
    try {
      const t = task.transcript
      const got = t?.run ? await focusApi.transcript(ws, t.run, { child: t.child ?? undefined, limit: 20 }) : null
      const first = got?.items.find(i => i.role === 'user' && i.kind === 'text' && i.text?.trim())?.text?.trim()
      setNote(`Continue as a separate session: ${[task.title, first].filter(Boolean).join(' - ').slice(0, 600)}`)
    } catch { setNote(task.title) }
    setBusy(false)
  }
  return { detach, startAnew, busy, message, note, close: () => setNote(null) }
}

export function TasksList({ ticket }: { ticket?: string }) {
  const data = useTasks(ticket)
  const action = useStop(data.ws)
  const separate = useSeparate(data.ws, ticket)
  const [delegating, setDelegating] = useState(false)
  return <section data-testid="focus-tasks-list" aria-label="Tasks" className="border-t border-border p-4 text-sm">
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="font-medium">Tasks</h2>
      {ticket && data.main && <Button variant="outline" size="sm" disabled={data.budget.max !== null && data.budget.used >= data.budget.max} title={data.budget.max !== null && data.budget.used >= data.budget.max ? 'The delegation limit of this session is reached.' : undefined} onClick={() => setDelegating(true)}>Delegate a task</Button>}
    </div>
    {delegating && ticket && data.main && <DelegateSheet ws={data.ws} ticket={ticket} parent={data.main} used={data.budget.used} max={data.budget.max} onClose={() => setDelegating(false)} />}
    {data.loading ? <p className="text-text-muted">Loading tasks...</p> : <>
      {data.limited && <p className="text-text-muted">{limitedText}</p>}
      {!data.limited && !data.tasks.length && <p className="text-text-muted">No tasks yet.</p>}
      <div className={data.tasks.length ? 'space-y-2 border-l-2 border-border pl-3' : ''}>{data.tasks.map(task => <details key={task.id} className="rounded-lg border border-border bg-surface text-xs [overflow-wrap:anywhere]">
        <summary className="cursor-pointer p-3 focus-visible:outline-2 focus-visible:outline-brand"><strong className="text-[13px]">{task.title}</strong><span className="ml-2 text-text-muted">{task.state}</span><small className="ml-3 mt-1 block text-text-muted">{task.origin}</small></summary>
        <div className="space-y-3 border-t border-border p-3 text-text-muted">
          <p>Result: {task.result || task.state}</p>
          <p>{task.harness} <span className="rounded border border-border px-2 py-0.5">{task.model || 'Model not reported'}</span></p>
          {(task.run?.started_at || task.startedAt) && <p>Started {new Date(task.run?.started_at ?? task.startedAt!).toLocaleString()}{task.endedAt ? `, ended ${new Date(task.endedAt).toLocaleString()}` : ''}</p>}
          {!!task.files?.length && <p data-testid="focus-task-files">Files: {task.files.join(', ')}</p>}
          <Button variant="outline" size="sm" disabled={action.busy || !task.run || !active(task.run)} onClick={() => { if (task.run && window.confirm('Stop this task?')) void action.stop([task.run]) }}>Stop</Button>
          {!task.run && task.state === 'running' && <p>This task stops with the main session.</p>}
          {task.spawnedBy === 'human' && task.run && <Button variant="outline" size="sm" disabled={separate.busy} onClick={() => void separate.detach(task.run!.run)}>Make separate session</Button>}
          {task.spawnedBy === 'agent' && ticket && <Button variant="outline" size="sm" disabled={separate.busy} onClick={() => void separate.startAnew(task)}>Start as separate session</Button>}
        </div>
      </details>)}</div>
    </>}
    {action.message && <p role="alert" className="mt-2 text-xs">{action.message}</p>}
    {separate.message && <p role="alert" className="mt-2 text-xs">{separate.message}</p>}
    {ticket && <StartAgentDialog ws={data.ws} ticket={ticket} open={separate.note !== null} choose initial={{ note: separate.note ?? undefined }} onClose={separate.close} />}
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
