// The history of one file (issue #491, decision 2): the commits that changed it, one commit's change, and the
// difference between two versions. A commit that a ticket's own events name carries that ticket as a chip (decision 3).
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { Link } from '@tanstack/react-router'
import { useState } from 'react'
import { explorerApi, explorerQueries, type Commit } from '@/api/explorer'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { fmtDateTime } from '@/lib/time'
import { Diff } from './Diff'

const PAGE = 50

function Chips({ commit }: { commit: Commit }) {
  return (
    <>
      {commit.tickets.map((t) => (
        <Link key={t.key} to="/ticket/$key" params={{ key: t.key }} className="rounded-full border border-border px-2 py-px text-[11px] text-brand hover:underline" title={t.title ?? undefined}>
          {t.key}
        </Link>
      ))}
    </>
  )
}

function CommitDiff({ ws, root, path, sha }: { ws: string; root: string; path: string; sha: string }) {
  const q = useQuery(explorerQueries.commit(ws, root, path, sha))
  if (q.isError) return <LoadFailed what="this change" onRetry={() => void q.refetch()} />
  if (!q.data) return <p className="text-[13px] text-text-muted">Loading the change…</p>
  return (
    <div className="space-y-2" data-testid="commit-diff">
      {q.data.body && <p className="whitespace-pre-wrap text-[13px] text-text-muted"><Inline>{q.data.body}</Inline></p>}
      {q.data.empty ? <p className="text-[13px] text-text-muted">This commit changed other files, not this one.</p> : <Diff patch={q.data.patch} label="Change in this commit" />}
      {q.data.truncated && <p className="text-[12px] text-warning">The change is long; only the first part is shown.</p>}
    </div>
  )
}

function CompareDiff({ ws, root, path, a, b }: { ws: string; root: string; path: string; a: string; b: string }) {
  const q = useQuery(explorerQueries.compare(ws, root, path, a, b))
  if (q.isError) return <LoadFailed what="this comparison" onRetry={() => void q.refetch()} />
  if (!q.data) return <p className="text-[13px] text-text-muted">Loading the comparison…</p>
  return q.data.empty ? <p className="text-[13px] text-text-muted">The two versions are the same.</p> : (
    <div className="space-y-2" data-testid="compare-diff">
      <Diff patch={q.data.patch} label="Difference between the two versions" />
      {q.data.truncated && <p className="text-[12px] text-warning">The difference is long; only the first part is shown.</p>}
    </div>
  )
}

export function HistoryPanel({ ws, root, path, sha, onSha, onOpenVersion }: { ws: string; root: string; path: string; sha?: string; onSha: (sha: string | undefined) => void; onOpenVersion: (sha: string) => void }) {
  const [picked, setPicked] = useState<string[]>([])
  const list = useInfiniteQuery({
    queryKey: ['explorer', ws, 'history', root, path],
    queryFn: ({ pageParam }) => explorerApi.history(ws, root, path, pageParam, PAGE),
    initialPageParam: 0,
    getNextPageParam: (last, all) => (last.more ? all.length * PAGE : undefined),
    retry: false,
  })
  if (list.isError) return <LoadFailed what="the history" onRetry={() => void list.refetch()} />
  if (!list.data) return <p role="status" className="text-[13px] text-text-muted">Loading the history…</p>
  const first = list.data.pages[0]
  if (!first.repo) return <p className="text-[13px] text-text-muted" data-testid="no-history">This file is not in a git repository, so there is no history.</p>
  const commits = list.data.pages.flatMap((p) => p.commits)
  if (commits.length === 0) return <p className="text-[13px] text-text-muted">No commit has changed this file yet.</p>
  // A third pick is not accepted silently: the other boxes are off until one of the two is cleared.
  const toggle = (s: string) => setPicked((p) => (p.includes(s) ? p.filter((x) => x !== s) : p.length >= 2 ? p : [...p, s]))
  // the older of the two picked versions first
  const order = picked.length === 2 ? [...picked].sort((x, y) => commits.findIndex((c) => c.sha === y) - commits.findIndex((c) => c.sha === x)) : []
  return (
    <div className="space-y-3">
      <p className="text-[12px] text-text-muted">Select a change title to view it. Select two checkboxes to compare versions.</p>
      <div className="flex min-h-6 flex-wrap items-center gap-2 text-[12px]" data-testid="pick-count">
        <span role="status" className={picked.length === 2 ? 'font-medium text-text' : 'text-text-muted'}>
          {picked.length} of 2 versions selected{picked.length === 2 ? '. To choose different ones, clear one first.' : ''}
        </span>
        {picked.length > 0 && <Button size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => setPicked([])}>Clear selection</Button>}
      </div>
      <ul aria-label="Changes to this file" className="divide-y divide-border rounded-md border border-border">
        {commits.map((c) => (
          <li key={c.sha} className="space-y-1 px-3 py-2 text-[13px]" data-sha={c.sha}>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="-m-1.5 flex size-10 shrink-0 items-center justify-center">
                <input type="checkbox" className="size-5 outline-none focus-visible:ring-2 focus-visible:ring-brand" aria-label={`Compare: ${c.subject}`} checked={picked.includes(c.sha)} disabled={picked.length >= 2 && !picked.includes(c.sha)} onChange={() => toggle(c.sha)} />
              </span>
              <button type="button" aria-expanded={sha === c.sha} className={`min-h-9 min-w-0 flex-1 basis-48 rounded py-1 text-left outline-none hover:underline focus-visible:ring-2 focus-visible:ring-brand ${sha === c.sha ? 'font-medium' : ''}`} onClick={() => onSha(sha === c.sha ? undefined : c.sha)}>
                <Inline>{c.subject || '(no message)'}</Inline>
              </button>
              <Chips commit={c} />
            </div>
            <p className="pl-8 text-[12px] text-text-muted">
              <Inline>{c.author}</Inline> · {fmtDateTime(c.at)}
            </p>
            {sha === c.sha && (
              <div className="space-y-2 pl-8">
                <CommitDiff ws={ws} root={root} path={path} sha={c.sha} />
                <Button size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => onOpenVersion(c.sha)}>
                  Open the file as it was then
                </Button>
                <InfoDrawer>
                  <span className="break-all font-mono">{c.sha}</span>
                </InfoDrawer>
              </div>
            )}
          </li>
        ))}
      </ul>
      {list.hasNextPage && (
        <Button size="sm" variant="outline" disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>
          Load more
        </Button>
      )}
      {order.length === 2 && (
        <section aria-label="Comparison" className="space-y-2">
          <h3 className="text-[12px] font-medium uppercase tracking-wide text-text-faint">Older version compared with the newer one</h3>
          <CompareDiff ws={ws} root={root} path={path} a={order[0]} b={order[1]} />
        </section>
      )}
    </div>
  )
}
