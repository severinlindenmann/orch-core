// The history of one file (issue #491, decision 2): the commits that changed it, one commit's change, and the
// difference between two versions. A commit that a ticket's own events name carries that ticket as a chip (decision 3).
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
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
  if (!list.data) return <p className="text-[13px] text-text-muted">Loading the history…</p>
  const first = list.data.pages[0]
  if (!first.repo) return <p className="text-[13px] text-text-muted" data-testid="no-history">This file is not in a git repository, so there is no history.</p>
  const commits = list.data.pages.flatMap((p) => p.commits)
  if (commits.length === 0) return <p className="text-[13px] text-text-muted">No commit has changed this file yet.</p>
  const toggle = (s: string) => setPicked((p) => (p.includes(s) ? p.filter((x) => x !== s) : p.length >= 2 ? [p[1], s] : [...p, s]))
  // the older of the two picked versions first
  const order = picked.length === 2 ? [...picked].sort((x, y) => commits.findIndex((c) => c.sha === y) - commits.findIndex((c) => c.sha === x)) : []
  return (
    <div className="space-y-3">
      <p className="text-[12px] text-text-muted">Pick a change to see it. Tick two to compare those versions.</p>
      <ul aria-label="Changes to this file" className="divide-y divide-border rounded-md border border-border">
        {commits.map((c) => (
          <li key={c.sha} className="space-y-1 px-3 py-2 text-[13px]" data-sha={c.sha}>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <input type="checkbox" aria-label={`Compare: ${c.subject}`} checked={picked.includes(c.sha)} onChange={() => toggle(c.sha)} />
              <button type="button" className={`min-w-0 flex-1 basis-48 text-left hover:underline ${sha === c.sha ? 'font-medium' : ''}`} onClick={() => onSha(sha === c.sha ? undefined : c.sha)}>
                <Inline>{c.subject || '(no message)'}</Inline>
              </button>
              <Chips commit={c} />
            </div>
            <p className="pl-6 text-[12px] text-text-muted">
              <Inline>{c.author}</Inline> · {fmtDateTime(c.at)}
            </p>
            {sha === c.sha && (
              <div className="space-y-2 pl-6">
                <CommitDiff ws={ws} root={root} path={path} sha={c.sha} />
                <Button size="sm" variant="ghost" className="h-7 text-[12px]" onClick={() => onOpenVersion(c.sha)}>
                  Open the file as it was then
                </Button>
                <details className="text-[12px] text-text-muted">
                  <summary className="cursor-pointer select-none">Technical details</summary>
                  <span className="break-all font-mono">{c.sha}</span>
                </details>
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
