import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GitCommitHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { diffstat } from '@/api/gates'
import { isRealHost } from '@/api/features'
import { ApiError } from '@/api/types'
import { useWorkspace } from '@/app/workspace'
import { can } from '@/api/permissions'
import { toastApiError } from '@/app/toast'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Diff } from './widgets/CoreViews'
import { fmtDay, Mono, Section, type TabProps } from './shared'

/** Statuses whose branch has work to look at: the Changes tab shows for these. */
export const HAS_CHANGES = new Set(['in-progress', 'waiting', 'testing', 'done'])

/**
 * Core's diff of the ticket branch against its base (owner decision 2026-10-10): what a verdict and a code review
 * sign, next to the evidence. The head commit is the one the verdict dialog names. Drawn by core with the diff
 * widget's renderer; nothing here comes from an addon.
 */
const BUSY_RETRIES = 4
const isBusy = (e: unknown): e is ApiError => e instanceof ApiError && e.code === 'quota.host'

export function Changes({ ticket, viewer, jump }: TabProps) {
  const qc = useQueryClient()
  const ws = useWorkspace().workspace?.id
  // The key carries the workspace and (mock) the branch head: a new commit reads the diff again. A real host's diff
  // names its own head commits, shown below, and a log item of the ticket invalidates it with the ticket.
  const changes = useQuery({
    queryKey: ['changes', ws, ticket.key, ticket.branch.head],
    queryFn: () => api.getChanges(ticket.key, ws),
    enabled: !!ws,
    // A real host reads one diff per person at a time (two host-wide) and answers `quota.host` while busy: wait the
    // time it names and try again, a bounded number of times, showing "busy" rather than an error.
    retry: (count, e) => isBusy(e) && count < BUSY_RETRIES,
    retryDelay: (count, e) => Math.min(isBusy(e) && e.retry_after_ms ? e.retry_after_ms : 1000 * 2 ** count, 8000),
  })
  const busy = changes.isFetching && isBusy(changes.failureReason)
  const push = useMutation({
    mutationFn: () => api.simulatePush(ticket.key),
    onSuccess: async (r) => {
      await qc.invalidateQueries()
      toast(`The agent pushed ${r.sha} to ${ticket.branch.name}`)
    },
    onError: (e) => toastApiError(e, 'Could not push'),
  })
  // The ticket's own branch facts (mock); a real host serves them with the diff instead.
  const diff = changes.data
  const b = ticket.branch.head ? ticket.branch : diff && { ...ticket.branch, name: diff.branch, base: diff.base, head: diff.head, files: diff.files.length, additions: diff.additions, deletions: diff.deletions }
  const signed = ticket.verdict?.source_sha
  // A verdict signs one commit per linked repo; with several there is no single head to compare with the first.
  const outdated = ticket.verdict?.current === false
  // Demo control: only where a verdict stands, so a push has something to void.
  const canDemo = !isRealHost() && can(viewer.role, 'ticket.act') && ticket.gates.verify.state === 'approved'

  return (
    <div className="space-y-4">
      <Section
        title={
          <span className="flex flex-wrap items-baseline gap-x-2">
            <span>Changes</span>
            {b && (
              <Mono className="text-text-muted">
                {b.name} against {b.base}
              </Mono>
            )}
          </span>
        }
        aside={
          <Button size="sm" variant="ghost" onClick={() => jump({ tab: 'acceptance' })}>
            Evidence
          </Button>
        }
      >
        {b ? (
          <p className="text-[13px] text-text">
            Head <Mono>{b.head}</Mono> · {diffstat(b)} in {b.files} file{b.files === 1 ? '' : 's'}
            {outdated && <span className="text-warning"> · the verdict is from before the latest change</span>}
            {signed && !outdated && (
              <span className={signed === b.head ? 'text-success' : 'text-warning'}>
                {' · '}
                {signed === b.head ? 'the verdict signed this commit' : `the verdict signed ${signed}, not the head`}
              </span>
            )}
          </p>
        ) : (
          <p className="text-[13px] text-text-muted">The branch is read from the host when the tab opens.</p>
        )}
        {diff?.sources && diff.sources.length > 1 && (
          <ul aria-label="Repositories" className="mt-1 space-y-0.5 text-[12px] text-text-muted">
            {diff.sources.map((x) => (
              <li key={x.repo}>
                <Mono className="text-text">{x.repo}</Mono> · {x.branch} · head <Mono>{x.head}</Mono> against <Mono>{x.base}</Mono>
              </li>
            ))}
          </ul>
        )}
        {diff?.truncated && <p className="mt-1 text-[12px] text-warning">The diff was cut at the host&apos;s size limit; the files below may be incomplete.</p>}
        <ol aria-label="Commits" className="mt-2 space-y-1">
          {ticket.branch.commits.map((c) => (
            <li key={c.sha} className="flex items-center gap-2 text-[12px] text-text-muted">
              <GitCommitHorizontal className="size-3.5 shrink-0" aria-hidden />
              <Mono className="text-text">{c.sha}</Mono>
              <span>{c.task ? `${c.task} · ` : ''}{viewer.name(c.by)}{c.at ? ` · ${fmtDay(c.at)}` : ''}</span>
            </li>
          ))}
        </ol>
        {canDemo && (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 text-[12px] text-text-muted">
            <span className="flex-1">Demo: what happens when the agent pushes after the verdict. Core voids the verdict and the ticket goes back to testing.</span>
            <Button size="sm" variant="outline" disabled={push.isPending} onClick={() => push.mutate()}>
              Simulate: the agent pushes a commit
            </Button>
          </div>
        )}
      </Section>
      {busy && (
        <p role="status" className="text-[13px] text-text-muted">
          The host is busy reading other diffs; trying again…
        </p>
      )}
      {changes.isLoading && <Skeleton className="h-40" />}
      {changes.error && !changes.isFetching && (
        <p role="alert" className="text-[13px] text-danger">
          {isBusy(changes.error) ? (
            <>
              The host is still busy reading other diffs.{' '}
              <Button type="button" variant="link" size="sm" className="h-auto p-0 text-[13px]" onClick={() => void changes.refetch()}>
                Try again
              </Button>
            </>
          ) : changes.error instanceof ApiError && changes.error.code === 'observe.unavailable' && /diff unavailable/.test(changes.error.message) ? (
            'Diff unavailable: git could not produce this diff (it failed or took too long). Nothing is shown rather than an incomplete diff.'
          ) : changes.error instanceof ApiError && changes.error.code === 'observe.unavailable' ? (
            'The host cannot show this branch right now: it moved since the host last looked, or has no observed head yet.'
          ) : (
            'Could not load the diff.'
          )}
        </p>
      )}
      {changes.data && (
        <div aria-label="Diff" className="space-y-3">
          {changes.data.files.map((f) => (
            <section key={`${f.repo ?? ''}:${f.path}`} aria-label={f.path}>
              <p className="mb-1 text-[12px] text-text-muted">
                <Mono className="text-text">{f.path}</Mono> · {diffstat(f)}{f.status && f.status !== 'M' ? ` · ${f.status === 'A' ? 'added' : f.status === 'D' ? 'deleted' : f.status === 'R' ? 'renamed' : 'changed'}` : ''}
              </p>
              <Diff f={{ file: f.path, lines: f.lines }} />
            </section>
          ))}
        </div>
      )}
    </div>
  )
}
