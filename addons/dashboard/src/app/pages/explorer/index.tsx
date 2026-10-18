// File Explorer (add-on, issue #491): a read-only look at this workspace's files and their git history. Owners only; the
// server checks that on every request, the page only hides what it would refuse. Nothing here can change a file.
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearch } from '@tanstack/react-router'
import { Search as SearchIcon } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { explorerQueries, type Hit } from '@/api/explorer'
import { hasFeature } from '@/api/features'
import { LoadFailed } from '@/components/LoadFailed'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'
import { useWorkspace } from '@/app/workspace'
import { Folder_ } from './Tree'
import { Viewer, type ViewerState } from './Viewer'

export interface ExplorerSearch {
  root?: string
  path?: string
  rev?: string
  view?: 'history'
  sha?: string
}

/** Debounced copy of a value (the search runs when typing pauses). */
function useDebounced<T>(value: T, ms = 300): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

function Results({ ws, q, mode, root, onPick }: { ws: string; q: string; mode: 'names' | 'text'; root: string | undefined; onPick: (root: string, path: string, line?: number) => void }) {
  const names = useQuery({ ...explorerQueries.names(ws, q, root), enabled: mode === 'names' })
  const text = useQuery({ ...explorerQueries.search(ws, q, root), enabled: mode === 'text' })
  const active = mode === 'names' ? names : text
  if (active.isError) return <LoadFailed what="the search" onRetry={() => void active.refetch()} />
  if (!active.data) return <p className="px-3 py-2 text-[13px] text-text-muted">Searching…</p>
  if (mode === 'names') {
    const found = names.data!
    if (found.files.length === 0) return <p className="px-3 py-2 text-[13px] text-text-muted">No file has that in its name.</p>
    return (
      <ul aria-label="Files found" className="divide-y divide-border">
        {found.files.map((f) => (
          <li key={`${f.root}:${f.path}`}>
            <button type="button" className="block w-full px-3 py-1.5 text-left text-[13px] hover:bg-surface-hover" onClick={() => onPick(f.root, f.path)}>
              <bdi className="break-all">{f.label}</bdi>
              {f.root !== 'ws' && <span className="ml-2 text-[11px] text-text-faint">{f.root.replace('repo:', '')}</span>}
            </button>
          </li>
        ))}
        {found.cut && <li className="px-3 py-1.5 text-[12px] text-text-faint">The folder is large; the search stopped early.</li>}
      </ul>
    )
  }
  const found = text.data!
  if (found.engine === null) return <p className="px-3 py-2 text-[13px] text-text-muted">No search tool is installed on this computer (ripgrep or git).</p>
  if (found.hits.length === 0) return <p className="px-3 py-2 text-[13px] text-text-muted">Nothing found.</p>
  return (
    <div>
      {found.engine === 'git' && <p className="px-3 py-1.5 text-[12px] text-text-faint">Searching saved files only (ripgrep is not installed).</p>}
      <ul aria-label="Matches" className="divide-y divide-border">
        {found.hits.map((h: Hit, i) => (
          <li key={`${h.root}:${h.path}:${h.line}:${i}`}>
            <button type="button" className="block w-full px-3 py-1.5 text-left hover:bg-surface-hover" onClick={() => onPick(h.root, h.path, h.line)}>
              <span className="block break-all text-[13px]">
                <bdi>{h.label}</bdi> <span className="text-text-faint">line {h.line}</span>
              </span>
              <bdi className="block truncate font-mono text-[12px] text-text-muted [unicode-bidi:isolate]">{h.text}</bdi>
            </button>
          </li>
        ))}
        {found.more && <li className="px-3 py-1.5 text-[12px] text-text-faint">More matches exist; narrow the search.</li>}
      </ul>
    </div>
  )
}

export function ExplorerPage() {
  usePageHeader('Files')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const served = !!ws && hasFeature('explorer', ws)
  const navigate = useNavigate()
  const search = useSearch({ strict: false }) as ExplorerSearch
  const info = useQuery({ ...explorerQueries.info(ws ?? ''), enabled: served })
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set(['ws:']))
  const [text, setText] = useState('')
  const [mode, setMode] = useState<'names' | 'text'>('names')
  const q = useDebounced(text.trim())
  const state = useMemo<ViewerState | null>(() => (search.path ? { root: search.root ?? 'ws', path: search.path, rev: search.rev, view: search.view, sha: search.sha } : null), [search])
  if (!workspace || !ws) return null
  if (!served || (info.data && !info.data.available)) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Files</h1>
        <p data-testid="explorer-unavailable" className="text-[13px] text-text-muted">
          The File Explorer is off, or only owners can use it.
        </p>
      </div>
    )
  }
  if (info.isError) return <LoadFailed what="the files" onRetry={() => void info.refetch()} />
  const go = (next: ExplorerSearch) => void navigate({ to: '/explorer', search: next, replace: false })
  const pick = (root: string, path: string) => go({ root, path })
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (!next.delete(key)) next.add(key)
      return next
    })
  const roots = info.data?.roots ?? []
  const searching = q.length > 0
  return (
    <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(260px,340px)_minmax(0,1fr)]" data-testid="explorer">
      <aside aria-label="Folders" className={cn('min-w-0 space-y-2', state && 'hidden lg:block')}>
        <div className="space-y-1.5">
          <label className="relative block">
            <span className="sr-only">Find</span>
            <SearchIcon className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-text-faint" aria-hidden />
            <input
              type="search"
              value={text}
              maxLength={200}
              onChange={(e) => setText(e.target.value)}
              placeholder={mode === 'names' ? 'Find a file by name' : 'Find text in files'}
              className="h-9 w-full rounded-md border border-border bg-surface pl-8 pr-2 text-[13px]"
            />
          </label>
          <div role="radiogroup" aria-label="Search in" className="flex gap-1 text-[12px]">
            {(['names', 'text'] as const).map((m) => (
              <button key={m} role="radio" aria-checked={mode === m} className={cn('rounded-full border px-2.5 py-0.5', mode === m ? 'border-brand bg-brand-soft' : 'border-border text-text-muted')} onClick={() => setMode(m)}>
                {m === 'names' ? 'File names' : 'Text in files'}
              </button>
            ))}
          </div>
        </div>
        {searching ? (
          <Results ws={ws} q={q} mode={mode} root={undefined} onPick={pick} />
        ) : (
          <div className="space-y-3">
            {roots.map((r) => (
              <section key={r.id} aria-label={r.label} className="space-y-1">
                <h2 className="px-2 text-[11px] font-medium uppercase tracking-wide text-text-faint">
                  <bdi>{r.label}</bdi>
                </h2>
                <Folder_ ws={ws} root={r.id} path="" depth={0} open={open} toggle={toggle} selected={state} onPick={pick} />
              </section>
            ))}
          </div>
        )}
      </aside>
      <div className={cn('min-w-0', !state && 'hidden lg:block')}>
        {state ? (
          <Viewer ws={ws} state={state} onChange={(patch) => go({ root: state.root, path: state.path, rev: state.rev, view: state.view, sha: state.sha, ...patch })} onBack={() => go({})} />
        ) : (
          <p className="text-[13px] text-text-muted">Choose a file to look at it. Nothing here can be changed.</p>
        )}
      </div>
    </div>
  )
}
