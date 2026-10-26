import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { worktreeDiffApi, worktreeDiffQueries, type WorktreeDiff } from '@/api/focus'
import { runQueries } from '@/api/runs'
import { worktreeQueries } from '@/api/worktrees'
import { Diff } from '../../ticket/widgets/CoreViews'
import { authorsOf, blockOf } from './authors'
import { Empty, useChanges, usePanelTicket } from './data'

function LiveChanges({ ws, ticket }: { ws: string; ticket: string }) {
  const runs = useQuery({ ...runQueries.list(ws), enabled: isRealHost(), retry: false, refetchInterval: 5000 })
  const running = !!runs.data?.runs.some(r => r.kind === 'agent' && r.ticket === ticket && r.state === 'running')
  const [choice, setChoice] = useState<boolean | null>(null)
  const live = choice ?? running
  const [open, setOpen] = useState<string | null>(null)
  const diff = useQuery<WorktreeDiff>({
    queryKey: worktreeDiffQueries.diffKey(ws, ticket), queryFn: () => worktreeDiffApi.get(ws, ticket), enabled: isRealHost() && live,
    retry: false, refetchInterval: live ? 3000 : false,
  })
  const authors = useQuery({ queryKey: worktreeDiffQueries.authorsKey(ws, ticket), queryFn: () => worktreeDiffApi.authors(ws, ticket), enabled: isRealHost() && live, retry: false, refetchInterval: live ? 5000 : false })
  const data = diff.data
  if (!isRealHost() && !data) return null
  return <section aria-label="Live worktree changes" data-testid="focus-live-changes" className="space-y-2 rounded border border-border p-2">
    <div className="flex items-center gap-2">
      <h3 className="flex-1 text-xs font-medium">Live changes in the worktree</h3>
      <button type="button" role="switch" aria-checked={live} aria-label="Live" onClick={() => setChoice(!live)} className="rounded border border-border px-2 py-1 text-xs aria-checked:bg-surface-2 aria-checked:text-brand">{live ? 'Live' : 'Paused'}</button>
    </div>
    {diff.isError && <p className="text-xs text-text-muted">Could not read the worktree.</p>}
    {!data && !diff.isError && <p className="text-xs text-text-muted">{live ? 'Reading the worktree…' : 'Paused. Turn Live on to read the worktree.'}</p>}
    {data && !data.available && <p className="text-xs text-text-muted">{data.reason ?? 'The worktree cannot be read now.'}</p>}
    {data?.available && <>
      {data.truncated && <p className="text-xs text-warning">Some changes are not shown (too many or too large).</p>}
      {!data.files.length && <p className="text-xs text-text-muted">The worktree has no uncommitted changes.</p>}
      {data.files.map(f => {
        const names = authors.data ? authorsOf(f.path, authors.data) : null
        const others = names && names.length > 1 ? names : null
        const block = blockOf(data.unified, f.path)
        const isOpen = open === f.path
        return <div key={f.path} data-testid="live-file">
          <button type="button" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : f.path)} className="flex w-full items-center gap-2 rounded border border-transparent px-2 py-2 text-left font-mono text-xs hover:bg-surface-2 aria-expanded:border-border aria-expanded:bg-surface-2">
            <span className="text-brand">{f.status}</span><span className="min-w-0 flex-1 break-all">{f.path}</span><span className="text-success">+{f.added}</span><span className="text-danger">-{f.removed}</span>
          </button>
          <p className="px-2 text-[11px] text-text-faint" data-testid="live-author">{names === null ? 'author unknown' : names.length ? `by ${names.join(', ')}` : 'author unknown'}</p>
          {others && <p role="alert" className="px-2 text-[11px] text-warning">Overlap: also changed by {others.slice(1).join(', ')}. Resolve before integration.</p>}
          {isOpen && <div className="my-3 min-w-0">{f.binary ? <p className="text-xs text-text-muted">Binary file. No text diff available.</p> : f.too_large ? <p className="text-xs text-text-muted">Too large to show.</p> : block ? <Diff f={{ file: f.path, lines: block }} /> : <p className="text-xs text-text-muted">No text changes to show.</p>}</div>}
        </div>
      })}
    </>}
  </section>
}

export function Changes({ ticket }: { ticket?: string }) {
  const detail = usePanelTicket(ticket)
  const changes = useChanges(ticket)
  const [selected, select] = useState<string | null>(null)
  const trees = useQuery({ ...worktreeQueries.ofTicket(detail.workspace?.id ?? '', ticket ?? ''), enabled: !!ticket && !!detail.workspace && isRealHost() })
  if (!ticket) return <Empty>No ticket yet. Changes will appear here.</Empty>
  if (changes.isError || detail.isError) return <Empty>Could not load changes.</Empty>
  if (!changes.data) return <Empty>Loading changes…</Empty>
  const groups = new Map<string, typeof changes.data.files>()
  for (const f of changes.data.files) {
    const directory = `${f.repo ? `${f.repo}/` : ''}${f.path.includes('/') ? f.path.slice(0, f.path.lastIndexOf('/')) : '.'}`
    groups.set(directory, [...(groups.get(directory) ?? []), f])
  }
  const dirty = trees.data?.available ? trees.data.rows.filter(r => r.ticket === ticket && r.dirty !== null) : []
  return <div data-testid="focus-panel-changes" className="space-y-4">
    {detail.workspace && <LiveChanges ws={detail.workspace.id} ticket={ticket} />}
    {dirty.length > 0 && <p className="text-xs text-text-muted">{dirty.reduce((n, r) => n + r.dirty!, 0)} uncommitted files in the worktree</p>}
    {changes.data.truncated && <p className="text-xs text-warning">The host shortened this diff. Some files may be missing.</p>}
    {!groups.size && <Empty>No changed files yet.</Empty>}
    {[...groups].map(([directory, files]) => <section key={directory} aria-label={directory}>
      <h3 className="mb-1 break-all font-mono text-xs text-text-faint">{directory}</h3>
      {files.map(f => {
        const key = `${ticket}:${f.repo ?? ''}:${f.path}`
        const open = key === selected
        const status = f.status?.[0]?.toUpperCase() ?? (/^new file mode/m.test(f.lines) ? 'A' : /^deleted file mode/m.test(f.lines) ? 'D' : 'M')
        return <div key={key}>
          <button type="button" aria-expanded={open} onClick={() => select(open ? null : key)} className="flex w-full items-center gap-2 rounded border border-transparent px-2 py-2 text-left font-mono text-xs hover:bg-surface-2 aria-expanded:border-border aria-expanded:bg-surface-2">
            <span className="text-brand">{status}</span><span className="min-w-0 flex-1 break-all">{f.path.split('/').at(-1)}</span><span className="text-success">+{f.additions}</span><span className="text-danger">-{f.deletions}</span>
          </button>
          {open && <div className="my-3 min-w-0">{f.binary ? <p className="text-xs text-text-muted">Binary file. No text diff available.</p> : <Diff f={{ file: f.path, lines: f.lines }} />}</div>}
        </div>
      })}
    </section>)}
  </div>
}
