import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { isRealHost } from '@/api/features'
import { worktreeQueries } from '@/api/worktrees'
import { Diff } from '../../ticket/widgets/CoreViews'
import { Empty, useChanges, usePanelTicket } from './data'

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
