import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GitCommitHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { diffstat } from '@/api/gates'
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
export function Changes({ ticket, viewer, jump }: TabProps) {
  const qc = useQueryClient()
  const changes = useQuery({ queryKey: ['changes', ticket.key, ticket.branch.head], queryFn: () => api.getChanges(ticket.key) })
  const push = useMutation({
    mutationFn: () => api.simulatePush(ticket.key),
    onSuccess: async (r) => {
      await qc.invalidateQueries()
      toast(`The agent pushed ${r.sha} to ${ticket.branch.name}`)
    },
    onError: (e) => toastApiError(e, 'Could not push'),
  })
  const b = ticket.branch
  const signed = ticket.verdict?.source_sha
  // Demo control: only where a verdict stands, so a push has something to void.
  const canDemo = can(viewer.role, 'ticket.act') && ticket.gates.verify.state === 'approved'

  return (
    <div className="space-y-4">
      <Section
        title={
          <span className="flex flex-wrap items-baseline gap-x-2">
            <span>Changes</span>
            <Mono className="text-text-muted">
              {b.name} against {b.base}
            </Mono>
          </span>
        }
        aside={
          <Button size="sm" variant="ghost" onClick={() => jump({ tab: 'acceptance' })}>
            Evidence
          </Button>
        }
      >
        <p className="text-[13px] text-text">
          Head <Mono>{b.head}</Mono> · {diffstat(b)} in {b.files} file{b.files === 1 ? '' : 's'}
          {signed && (
            <span className={signed === b.head ? 'text-success' : 'text-warning'}>
              {' · '}
              {signed === b.head ? 'the verdict signed this commit' : `the verdict signed ${signed}, not the head`}
            </span>
          )}
        </p>
        <ol aria-label="Commits" className="mt-2 space-y-1">
          {b.commits.map((c) => (
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
      {changes.isLoading && <Skeleton className="h-40" />}
      {changes.error && <p role="alert" className="text-[13px] text-danger">Could not load the diff.</p>}
      {changes.data && (
        <div aria-label="Diff" className="space-y-3">
          {changes.data.files.map((f) => (
            <section key={f.path} aria-label={f.path}>
              <p className="mb-1 text-[12px] text-text-muted">
                <Mono className="text-text">{f.path}</Mono> · {diffstat(f)}
              </p>
              <Diff f={{ file: f.path, lines: f.lines }} />
            </section>
          ))}
        </div>
      )}
    </div>
  )
}
