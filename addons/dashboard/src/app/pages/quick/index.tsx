// Quick tasks on a real host (M6, host contract 12.9, run 8 V2): one input to add, then the list. Each row says in plain
// words where the task is (Not started, Working, Ready for your review, Needs your answer, Couldn't start, Finished) and
// has the one button that fits. Size is not asked: a row only says "Bigger than a quick task" when the host measured it.
import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { MoreHorizontal } from 'lucide-react'
import { hasFeature } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { ACTIVE_STATUSES, DONE_STATUSES, QUICK_LABEL, type QuickRow } from '@/api/quickRecords'
import { queries } from '@/api/queries'
import type { HandoffAction } from '@/api/handoff'
import { AgentName } from '@/components/agents/AgentIcon'
import { LoadFailed } from '@/components/LoadFailed'
import { SignPopover } from '@/components/sign/SignPopover'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { harnessForAgent } from '@/api/harnesses'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { cn } from '@/lib/utils'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { AddQuick } from './AddQuick'
import { taskState, type TaskState } from './status'

type Group = 'active' | 'done'

/**
 * The labels a ticket keeps when it stops being a quick task: all but `quick`. There is no remove-label op, so "make a normal
 * ticket" is `details` with `labels` set to these (replace-only), including an empty set.
 */
export const remainingLabels = (labels: readonly string[]): string[] => labels.filter((l) => l !== QUICK_LABEL)

const TONE: Record<TaskState['tone'], string> = {
  quiet: 'border-border text-text-muted',
  live: 'border-success/40 bg-success/10 text-success',
  attention: 'border-warning/50 text-warning',
  bad: 'border-danger/50 text-danger',
}

function Row({ row, state, canAct, onStart, onClose, onNormal }: { row: QuickRow; state: TaskState; canAct: boolean; onStart: (key: string) => void; onClose: (key: string, at: HTMLElement | null) => void; onNormal: (key: string, at: HTMLElement | null) => void }) {
  const finished = row.status === 'done' || row.status === 'closed'
  const harness = row.claim?.agent ? harnessForAgent(row.claim.agent) : null
  const known = harness === 'claude-code' || harness === 'codex'
  const over = row.quick?.over === true
  const menuAt = useRef<HTMLElement | null>(null)
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1.5 py-2.5 text-[13px]" data-key={row.key}>
      <Link to="/ticket/$key" params={{ key: row.key }} className="font-mono text-[12px] text-brand hover:underline">
        {row.key}
      </Link>
      <span className="min-w-0 flex-1 basis-48">
        <Inline>{row.title}</Inline>
        {over && <span data-testid="quick-over" className="ml-2 rounded-full border border-warning/50 px-2 py-px text-[11px] text-warning">Bigger than a quick task</span>}
      </span>
      {known && <AgentName harness={harness} className="text-[12px] text-text-muted" />}
      <span data-testid="quick-status" className={cn('rounded-full border px-2 py-px text-[11px]', TONE[state.tone])}>{state.word}</span>
      {state.action === 'start' || state.action === 'retry' ? (
        <Button size="sm" variant="outline" disabled={!canAct} onClick={() => onStart(row.key)}>{state.actionLabel}</Button>
      ) : state.action === 'review' ? (
        <Button size="sm" variant="outline" asChild><Link to="/ticket/$key" params={{ key: row.key }}>{state.actionLabel}</Link></Button>
      ) : state.action === 'answer' ? (
        <Button size="sm" variant="outline" asChild><Link to="/ticket/$key" params={{ key: row.key }} search={{ tab: 'questions' } as never}>{state.actionLabel}</Link></Button>
      ) : null}
      {canAct && !finished && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button size="icon-sm" variant="ghost" aria-label={`More for ${row.key}`} onPointerDown={(e) => { menuAt.current = e.currentTarget }}><MoreHorizontal /></Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {row.labels?.includes(QUICK_LABEL) && <DropdownMenuItem onSelect={() => onNormal(row.key, menuAt.current)}>Make a normal ticket</DropdownMenuItem>}
            <DropdownMenuItem onSelect={() => onClose(row.key, menuAt.current)}>Close</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </li>
  )
}

/** A row for a task this page just created, until the list has it. */
const placeholder = (key: string, title: string): QuickRow =>
  ({ key, uid: '', title, type: 'chore', priority: null, size: null, status: 'open', labels: [QUICK_LABEL], parent: null, owner: null, assignees: [], claim: null, turn: null, progress: null, open_questions: 0, blocking_questions: null, restricted: null, awaiting_gate: null, addons: {}, updated_at: '', quick: null }) as unknown as QuickRow

/** Quick tasks switched off in this workspace: the whole page is this one explanation (the tickets stay readable). */
function QuickOff() {
  return (
    <div className="max-w-2xl space-y-3" data-testid="quick-off">
      <h1 className="text-xl font-semibold tracking-tight">Quick tasks is off</h1>
      <p className="text-[13px] text-text-muted">
        Nobody in this workspace can make a new quick task while it is off, not from the dashboard, the command line or an
        agent. The quick tasks you already have stay as ordinary tickets and you can still open them.
      </p>
      <p className="text-[13px] text-text-muted">An owner can switch it on again in Settings, Add-ons.</p>
      <Button asChild variant="outline" size="sm">
        <Link to="/addons">Open Add-ons</Link>
      </Button>
    </div>
  )
}

export function QuickPage() {
  usePageHeader('Quick tasks')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const qc = useQueryClient()
  const [group, setGroup] = useState<Group>('active')
  const [closing, setClosing] = useState<(Extract<HandoffAction, { action: 'close' | 'details' }> & { at: HTMLElement | null }) | null>(null)
  const [starting, setStarting] = useState<Record<string, string>>({})
  const [failed, setFailed] = useState<Set<string>>(new Set())
  const [startFor, setStartFor] = useState<string | null>(null)
  const [reading, setReading] = useState(false)
  const [readError, setReadError] = useState(false)
  const makeNormal = async (key: string, at: HTMLElement | null) => {
    if (!ws || reading) return
    setReading(true)
    setReadError(false)
    try {
      const ticket = await qc.fetchQuery({ ...queries.ticket(ws, key), staleTime: 0 })
      const labels = remainingLabels(ticket.labels)
      if (!ticket.labels.includes(QUICK_LABEL) || ['done', 'closed'].includes(ticket.status)) {
        setReadError(true)
        return
      }
      setClosing({ action: 'details', key, labels, at })
    } catch {
      setReadError(true)
    } finally {
      setReading(false)
    }
  }
  const served = !!ws && hasFeature('quick', ws)
  const list = useQuery({ ...queries.quick(ws ?? '', group === 'active' ? ACTIVE_STATUSES : DONE_STATUSES), enabled: served })
  const other = useQuery({ ...queries.quick(ws ?? '', group === 'active' ? DONE_STATUSES : ACTIVE_STATUSES), enabled: served })
  if (!workspace || !ws) return null
  if (!served || (list.data && !list.data.available)) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Quick tasks</h1>
        <p data-testid="quick-unavailable" className="text-[13px] text-text-muted">
          This host does not serve quick tasks, so there is nothing to list here.
        </p>
      </div>
    )
  }
  if (hasFeature('quick', ws) && !hasFeature('quick_on', ws)) return <QuickOff />
  if (list.isError) return <LoadFailed what="quick tasks" onRetry={() => void list.refetch()} />
  const listed = list.data?.rows ?? []
  // A task this page just created is in the list at once (optimistic), until the host's own list has it.
  const extra = group === 'active' ? Object.entries(starting).filter(([k]) => !listed.some((r) => r.key === k)).map(([k, t]) => placeholder(k, t)) : []
  const sorted = [...extra, ...listed]
  const counts = { active: group === 'active' ? (list.data?.total ?? 0) + extra.length : other.data?.total, done: group === 'done' ? list.data?.total : other.data?.total }
  const mayAdd = atLeast(role, 'member')

  return (
    <div className="w-full max-w-4xl space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Quick tasks</h1>
      </div>
      <p className="text-[13px] text-text-muted">Small jobs an agent can do right away.</p>

      {mayAdd && (
        <AddQuick
          ws={ws}
          onCreated={(key, title) => setStarting((s) => ({ ...s, [key]: title }))}
          onStartFailed={(key) => setFailed((f) => new Set(f).add(key))}
          onStartDone={(key) => { setStarting((s) => { const n = { ...s }; delete n[key]; return n }); void qc.invalidateQueries({ queryKey: ['quick', ws] }) }}
        />
      )}

      <div role="tablist" aria-label="Quick tasks" className="flex gap-1 border-b border-border">
        {(['active', 'done'] as const).map((g) => (
          <button
            key={g}
            role="tab"
            type="button"
            aria-selected={group === g}
            onClick={() => setGroup(g)}
            className={cn('-mb-px border-b-2 px-3 py-1.5 text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-brand', group === g ? 'border-brand text-text' : 'border-transparent text-text-muted hover:text-text')}
          >
            {g === 'active' ? 'Active' : 'Finished'} {counts[g] != null && <span className="ml-1 text-text-faint">{counts[g]}</span>}
          </button>
        ))}
      </div>

      {list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the quick tasks…
        </p>
      ) : sorted.length === 0 ? (
        <p data-testid="quick-empty" className="text-[13px] text-text-muted">
          {group === 'active' ? 'No quick tasks are waiting.' : 'No quick task has been finished yet.'}
        </p>
      ) : (
        <ul aria-label={group === 'active' ? 'Active quick tasks' : 'Finished quick tasks'} className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
          {sorted.map((r) => (
            <Row
              key={r.key}
              row={r}
              state={taskState(r, { starting: r.key in starting, failed: failed.has(r.key) })}
              canAct={mayAdd && !reading}
              onStart={(key) => { setFailed((f) => { const n = new Set(f); n.delete(key); return n }); setStartFor(key) }}
              onClose={(key, at) => setClosing({ action: 'close', key, at })}
              onNormal={(key, at) => void makeNormal(key, at)}
            />
          ))}
        </ul>
      )}
      {list.data?.next_page && <p className="text-[12px] text-text-muted">More quick tasks exist than this page lists. Open the Tickets page filtered by the label quick to see all of them.</p>}

      {reading && <p role="status">Reading the ticket’s current labels…</p>}
      {readError && <p role="alert">Could not prepare conversion from the current ticket. Reload the quick tasks and try again.</p>}

      {startFor && <StartAgentDialog ws={ws} ticket={startFor} open onClose={() => setStartFor(null)} onStarted={() => void qc.invalidateQueries({ queryKey: ['quick', ws] })} />}
      {closing && (
        <SignPopover
          key={closing.action + closing.key}
          summary={closing.action === 'details' ? <div><p>Labels removed: <Inline>{QUICK_LABEL}</Inline></p><p>Labels kept: {closing.labels?.length ? closing.labels.map((label) => <span key={label} className="mr-2"><Inline>{label}</Inline></span>) : 'none'}</p></div> : undefined}
          title={closing.action === 'details' ? `Make ${closing.key} a normal ticket` : `Close ${'key' in closing ? closing.key : ''}`}
          ws={ws}
          anchor={closing.at}
          action={(({ at: _at, ...a }) => a)(closing)}
          doneToast={closing.action === 'details' ? `${closing.key} is a normal ticket now` : `${closing.key} is closed`}
          autoStart
          onClose={() => setClosing(null)}
          onSettled={(phase) => phase === 'done' && void qc.invalidateQueries({ queryKey: ['quick', ws] })}
        />
      )}
    </div>
  )
}
