// Quick tasks on a real host (M6, host contract 12.9): the list is `quick.list` (a quick task is a chore ticket with the
// label `quick`), its limits are this browser's own setting, and adding one is the workspace's ONE signing path.
import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { toast } from 'sonner'
import { hasFeature } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { ACTIVE_STATUSES, DONE_STATUSES, LIMIT_RANGE, QUICK_LABEL, readQuickLimits, writeQuickLimits, type QuickLimits, type QuickRow } from '@/api/quickRecords'
import { queries } from '@/api/queries'
import type { HandoffAction } from '@/api/handoff'
import { LoadFailed } from '@/components/LoadFailed'
import { SignPopover } from '@/components/sign/SignPopover'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import { plural } from '@/lib/time'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { usePageHeader } from '@/app/shell/ShellUi'
import { statusLabel } from '../ticket/shared'
import { AddQuick } from './AddQuick'

type Group = 'active' | 'done'

/** "4 commits, 7 files"; a count the host could not read is "unknown", never zero. */
export function countsText(q: QuickRow['quick']): string {
  if (!q || (q.commits === null && q.files === null)) return 'size not measured yet'
  return `${q.commits === null ? 'commits unknown' : plural(q.commits, 'commit')}, ${q.files === null ? 'files unknown' : plural(q.files, 'file')}`
}

function Limits({ ws, limits, onChange }: { ws: string; limits: QuickLimits; onChange: (l: QuickLimits) => void }) {
  const [draft, setDraft] = useState({ max_commits: String(limits.max_commits ?? ''), max_files: String(limits.max_files ?? '') })
  const parse = (v: string, key: keyof QuickLimits): number | null | 'bad' => {
    if (v.trim() === '') return null
    const n = Number(v)
    return Number.isInteger(n) && n >= LIMIT_RANGE[key].min && n <= LIMIT_RANGE[key].max ? n : 'bad'
  }
  const commits = parse(draft.max_commits, 'max_commits')
  const files = parse(draft.max_files, 'max_files')
  const bad = commits === 'bad' || files === 'bad'
  const save = () => {
    if (commits === 'bad' || files === 'bad') return
    const next = { max_commits: commits, max_files: files }
    if (!writeQuickLimits(ws, next)) toast('This browser would not keep the limits; they last for this page only.')
    onChange(next)
  }
  return (
    <details data-testid="quick-limits" className="rounded-md border border-border bg-surface px-3 py-2 text-[13px]">
      <summary className="cursor-pointer select-none text-text-muted hover:text-text">When is a quick task too big?</summary>
      <div className="mt-3 space-y-3">
        <p className="text-text-muted">A quick task that changes more commits or files than these is marked "too big" and should become a normal ticket. Kept in this browser only, for this workspace; empty means the host's default.</p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="space-y-1.5">
            <Label htmlFor="quick-max-commits">Commits (1 to {LIMIT_RANGE.max_commits.max})</Label>
            <Input id="quick-max-commits" type="number" className="w-28" min={1} max={LIMIT_RANGE.max_commits.max} step={1} value={draft.max_commits} aria-invalid={commits === 'bad' ? true : undefined} onChange={(e) => setDraft({ ...draft, max_commits: e.target.value })} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="quick-max-files">Files (1 to {LIMIT_RANGE.max_files.max})</Label>
            <Input id="quick-max-files" type="number" className="w-28" min={1} max={LIMIT_RANGE.max_files.max} step={1} value={draft.max_files} aria-invalid={files === 'bad' ? true : undefined} onChange={(e) => setDraft({ ...draft, max_files: e.target.value })} />
          </div>
          <Button size="sm" disabled={bad} onClick={save}>
            Save limits
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setDraft({ max_commits: '', max_files: '' })
              writeQuickLimits(ws, { max_commits: null, max_files: null })
              onChange({ max_commits: null, max_files: null })
            }}
          >
            Use the host's defaults
          </Button>
        </div>
        {bad && (
          <p role="alert" className="text-danger">
            Commits are a whole number from 1 to {LIMIT_RANGE.max_commits.max}, files from 1 to {LIMIT_RANGE.max_files.max}.
          </p>
        )}
      </div>
    </details>
  )
}

/**
 * The labels a ticket keeps when it stops being a quick task: all but `quick`. There is no remove-label op, so "make a normal
 * ticket" is `details` with `labels` set to these (replace-only), including an empty set.
 */
export const remainingLabels = (labels: readonly string[]): string[] => labels.filter((l) => l !== QUICK_LABEL)

function Row({ row, canClose, onClose, onNormal }: { row: QuickRow; canClose: boolean; onClose: (key: string) => void; onNormal: (key: string) => void }) {
  const over = row.quick?.over === true
  const finished = row.status === 'done' || row.status === 'closed'
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-[13px]" data-key={row.key}>
      <Link to="/ticket/$key" params={{ key: row.key }} className="font-mono text-[12px] text-brand hover:underline">
        {row.key}
      </Link>
      <span className="min-w-0 flex-1 basis-48">
        <Inline>{row.title}</Inline>
      </span>
      <span className="rounded-full border border-border px-2 py-px text-[11px] text-text-muted">{statusLabel(row.status)}</span>
      <span className={cn('text-[12px]', over ? 'text-warning' : 'text-text-faint')} data-testid="quick-counts">
        {over && <span className="mr-1.5 rounded bg-warning-soft px-1.5 py-px font-medium text-warning">too big</span>}
        {countsText(row.quick)}
      </span>
      {canClose && !finished && row.labels?.includes(QUICK_LABEL) && (
        <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Make ${row.key} a normal ticket`} onClick={() => onNormal(row.key)}>
          Make a normal ticket
        </Button>
      )}
      {canClose && !finished && (
        <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Close ${row.key}`} onClick={() => onClose(row.key)}>
          Close
        </Button>
      )}
    </li>
  )
}

export function QuickPage() {
  usePageHeader('Quick tasks')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const role = useRole()
  const qc = useQueryClient()
  const [group, setGroup] = useState<Group>('active')
  const [limits, setLimits] = useState<QuickLimits>(() => (ws ? readQuickLimits(ws) : { max_commits: null, max_files: null }))
  const [closing, setClosing] = useState<Extract<HandoffAction, { action: 'close' | 'details' }> | null>(null)
  const [reading, setReading] = useState(false)
  const [readError, setReadError] = useState(false)
  const makeNormal = async (key: string) => {
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
      setClosing({ action: 'details', key, labels })
    } catch {
      setReadError(true)
    } finally {
      setReading(false)
    }
  }
  const served = !!ws && hasFeature('quick', ws)
  const list = useQuery({ ...queries.quick(ws ?? '', group === 'active' ? ACTIVE_STATUSES : DONE_STATUSES, limits), enabled: served })
  const other = useQuery({ ...queries.quick(ws ?? '', group === 'active' ? DONE_STATUSES : ACTIVE_STATUSES, limits), enabled: served })
  if (!workspace || !ws) return null
  if (!served) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Quick tasks</h1>
        <p data-testid="quick-unavailable" className="text-[13px] text-text-muted">
          This host does not serve quick tasks, so there is nothing to list here.
        </p>
      </div>
    )
  }
  if (list.isError) return <LoadFailed what="quick tasks" onRetry={() => void list.refetch()} />
  if (list.data && !list.data.available) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Quick tasks</h1>
        <p data-testid="quick-unavailable" className="text-[13px] text-text-muted">
          This host does not serve quick tasks, so there is nothing to list here.
        </p>
      </div>
    )
  }
  const rows = list.data?.rows ?? []
  const counts = { active: group === 'active' ? list.data?.total : other.data?.total, done: group === 'done' ? list.data?.total : other.data?.total }
  // Outgrown tasks come first, then the host's order.
  const sorted = [...rows].sort((a, b) => Number(b.quick?.over === true) - Number(a.quick?.over === true))
  const mayAdd = atLeast(role, 'member')

  return (
    <div className="w-full max-w-4xl space-y-4">
      <div className="flex items-center gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Quick tasks</h1>
      </div>
      <p className="text-[13px] text-text-muted">One-line jobs too small for a full ticket. A quick task is a chore ticket with the label quick; an agent picks it up on its own.</p>

      {mayAdd && <AddQuick ws={ws} />}

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
            {g === 'active' ? 'Active' : 'Completed'} {counts[g] != null && <span className="ml-1 text-text-faint">{counts[g]}</span>}
          </button>
        ))}
      </div>

      {list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the quick tasks…
        </p>
      ) : sorted.length === 0 ? (
        <p data-testid="quick-empty" className="text-[13px] text-text-muted">
          {group === 'active' ? 'No quick tasks are waiting.' : 'No quick task has been completed yet.'}
        </p>
      ) : (
        <ul aria-label={group === 'active' ? 'Active quick tasks' : 'Completed quick tasks'} className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
          {sorted.map((r) => (
            <Row key={r.key} row={r} canClose={mayAdd && !reading} onClose={(key) => setClosing({ action: 'close', key })} onNormal={(key) => void makeNormal(key)} />
          ))}
        </ul>
      )}
      {list.data?.next_page && <p className="text-[12px] text-text-muted">More quick tasks exist than this page lists. Open the Tickets page filtered by the label quick to see all of them.</p>}

      {reading && <p role="status">Reading the ticket’s current labels…</p>}
      {readError && <p role="alert">Could not prepare conversion from the current ticket. Reload the quick tasks and try again.</p>}
      <Limits ws={ws} limits={limits} onChange={setLimits} />

      {closing && (
        <SignPopover
          key={closing.action + closing.key}
          summary={closing.action === 'details' ? <div><p>Labels removed: <Inline>{QUICK_LABEL}</Inline></p><p>Labels kept: {closing.labels?.length ? closing.labels.map((label) => <span key={label} className="mr-2"><Inline>{label}</Inline></span>) : 'none'}</p></div> : undefined}
          title={closing.action === 'details' ? `Make ${closing.key} a normal ticket` : `Close ${'key' in closing ? closing.key : ''}`}
          ws={ws}
          action={closing}
          autoStart
          onClose={() => setClosing(null)}
          onSettled={(phase) => phase === 'done' && void qc.invalidateQueries({ queryKey: ['quick', ws] })}
        />
      )}
    </div>
  )
}
