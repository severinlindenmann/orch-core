// Worktrees on a real host (H11d, host contract 12.7): one git worktree per ticket and repository. The list is
// `worktree.list` (what the host's reader sees in each folder now); adding and removing are the host's own writes.
import { useQuery } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { hasFeature } from '@/api/features'
import { atLeast } from '@/api/permissions'
import { changesText, STATE_TEXT, worktreeQueries, type Worktree } from '@/api/worktrees'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { fmtDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useRole } from '@/app/useRole'
import { useWorkspace } from '@/app/workspace'
import { PreviewDialog } from '@/app/terminal/real/PreviewDialog'
import { useOpenTerminal } from '@/app/terminal/real/useOpenTerminal'
import { CreateWorktree, RemoveWorktree } from './Dialogs'

const who = (w: Worktree) => (w.mine ? 'you' : w.by === 'agent' ? 'an agent' : w.by === 'person' ? 'a person' : 'unknown')

function Row({ w, canRemove, onRemove, onTerminal }: { w: Worktree; canRemove: boolean; onRemove: () => void; onTerminal?: () => void }) {
  const s = STATE_TEXT[w.state]
  return (
    <li className="space-y-1 py-3 text-[13px]" data-ticket={w.ticket ?? ''}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {w.ticket && (
          <Link to="/ticket/$key" params={{ key: w.ticket }} className="font-mono text-[12px] text-brand hover:underline">
            {w.ticket}
          </Link>
        )}
        <span className="min-w-0 flex-1 basis-48">{w.title ? <Inline>{w.title}</Inline> : <span className="text-text-faint">title not shown</span>}</span>
        <span className={cn('rounded-full border px-2 py-px text-[11px]', s.usable ? 'border-border text-text-muted' : 'border-warning text-warning')} title={s.detail}>
          {s.label}
        </span>
        {onTerminal && <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Open a terminal in the worktree of ${w.ticket}`} onClick={onTerminal}>Open terminal here</Button>}
        {canRemove && (
          <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`Remove the worktree of ${w.ticket ?? 'this ticket'}`} onClick={onRemove}>
            Remove
          </Button>
        )}
      </div>
      <p className="text-text-muted">
        {w.repo ? <Inline>{w.repo}</Inline> : 'repository not shown'} · {changesText(w)}
        {w.created_at ? ` · made ${fmtDateTime(w.created_at)} by ${who(w)}` : ''}
      </p>
      {!s.usable && <p className="text-warning">{s.detail}</p>}
      <details className="text-[12px] text-text-muted">
        <summary className="cursor-pointer select-none">Technical details</summary>
        <dl className="mt-1 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
          <dt>Branch</dt>
          <dd className="break-all font-mono">{w.branch ? <Inline>{w.branch}</Inline> : 'not shown'}</dd>
          <dt>Folder</dt>
          <dd className="break-all font-mono">{w.path ? <Inline>{w.path}</Inline> : 'not shown'}</dd>
        </dl>
      </details>
    </li>
  )
}

export function WorktreesPage() {
  usePageHeader('Worktrees')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const terminal = useOpenTerminal(ws ?? '')
  const role = useRole()
  const [adding, setAdding] = useState(false)
  const [removing, setRemoving] = useState<Worktree | null>(null)
  const served = !!ws && hasFeature('worktrees', ws)
  const list = useQuery({ ...worktreeQueries.list(ws ?? ''), enabled: served })
  if (!workspace || !ws) return null
  if (!served || (list.data && !list.data.available)) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Worktrees</h1>
        <p data-testid="worktrees-unavailable" className="text-[13px] text-text-muted">
          This host does not serve worktrees, so there is nothing to list here.
        </p>
      </div>
    )
  }
  if (list.isError) return <LoadFailed what="worktrees" onRetry={() => void list.refetch()} />
  const rows = list.data?.rows ?? []
  const mayChange = atLeast(role, 'member')
  return (
    <div className="w-full max-w-4xl space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Worktrees</h1>
        {mayChange && (
          <Button size="sm" className="ml-auto" onClick={() => setAdding(true)}>
            Add worktree
          </Button>
        )}
      </div>
      <p className="text-[13px] text-text-muted">
        A worktree is a separate folder where one ticket’s work happens, so tickets never mix in the same files. A ticket without one works in the repository’s shared working copy. Removing a worktree deletes the folder, never the branch.
      </p>
      {list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the worktrees…
        </p>
      ) : rows.length === 0 ? (
        <p data-testid="worktrees-empty" className="text-[13px] text-text-muted">
          No ticket has its own worktree yet. Agents get one when they start work on a ticket, or you can add one.
        </p>
      ) : (
        <ul aria-label="Worktrees" className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
          {rows.map((w) => (
            <Row key={`${w.ticket}/${w.repo}`} w={w} canRemove={mayChange} onRemove={() => setRemoving(w)} onTerminal={w.ticket && w.repo && STATE_TEXT[w.state].usable && mayChange && hasFeature('terminals_page', ws) ? () => void terminal.ask({ ticket: w.ticket!, cwd: { kind: 'worktree', repo: w.repo!, ticket: w.ticket! } }) : undefined} />
          ))}
        </ul>
      )}
      <PreviewDialog {...terminal} />
      {adding && <CreateWorktree ws={ws} onClose={() => setAdding(false)} />}
      {removing && <RemoveWorktree ws={ws} row={removing} onClose={() => setRemoving(null)} />}
    </div>
  )
}
