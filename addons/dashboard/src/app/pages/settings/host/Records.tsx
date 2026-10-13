// Records on a real host (M6, host contract 12.8 and 12.11): the status the host gives owners and maintainers, the two
// things the dashboard may run (commit [--push] and auto off, each a fixed child of the server), and, as exact commands
// and never as buttons, what only a terminal may do. Everything the host or the child wrote is drawn through visible.tsx.
import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { atLeast } from '@/api/permissions'
import { EXPORT_WARNING, PRIVATE_REMOTE, TERMINAL_COMMANDS, type RecordsRun, type RecordsStatus } from '@/api/quickRecords'
import { queries } from '@/api/queries'
import { api } from '@/api/client'
import { ApiError, type Workspace } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { fmtDateTime, plural } from '@/lib/time'
import { displayName, Mono, Section } from '../../ticket/shared'
import { useViewerRole } from './useViewerRole'

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[140px_1fr] items-start gap-3 py-1.5 text-[13px]">
      <dt className="text-text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  )
}

/** "Push publishes every ticket in this workspace to <url>, restricted ones included." with the URL exact. */
export function ExportWarning({ url }: { url: string | null }) {
  if (url === null)
    return (
      <p data-testid="records-export-warning" className="text-[13px] text-warning">
        A push publishes every ticket in this workspace, restricted ones included. The address it goes to has characters that cannot be shown here, so it is not shown.
      </p>
    )
  return (
    <p data-testid="records-export-warning" className="text-[13px] text-warning">
      {EXPORT_WARNING.before}
      <Raw>{url}</Raw>
      {EXPORT_WARNING.after}
    </p>
  )
}

/** "orch trusts your word that only you read <url>." */
export function PrivateRemoteLine({ url }: { url: string }) {
  return (
    <p data-testid="records-private-line" className="text-[13px] text-text-muted">
      {PRIVATE_REMOTE.before}
      <Raw>{url}</Raw>
      {PRIVATE_REMOTE.after}
    </p>
  )
}

function Command({ children, what }: { children: string; what: string }) {
  return (
    <li className="space-y-0.5">
      <code className="block break-all rounded bg-surface-3 px-2 py-1 font-mono text-[12px] text-text">{children}</code>
      <span className="text-[12px] text-text-muted">{what}</span>
    </li>
  )
}

/** A failed run: the stable code always, the child's message only when it was safe to show, and the commit that did land. */
export function RunFailure({ error }: { error: ApiError }) {
  return (
    <div role="alert" data-testid="records-failure" className="space-y-1 rounded-md border border-danger/40 px-3 py-2 text-[13px]">
      <p>
        <span className="font-medium text-danger">Not done.</span> <Mono>{error.code}</Mono>
        {error.message_hidden || error.message === error.code ? '' : <>: <Inline>{error.message}</Inline></>}
      </p>
      {error.message_hidden && <p className="text-text-muted">The records tool wrote text that cannot be shown here, so only its code is shown.</p>}
      {error.commit && (
        <p className="text-text-muted">
          A local commit already landed: <Mono>{error.commit}</Mono>. Only the push failed.
        </p>
      )}
      {error.retryable && <p className="text-text-muted">This can be tried again.</p>}
    </div>
  )
}

function RunResult({ run }: { run: RecordsRun }) {
  return (
    <p role="status" data-testid="records-result" className="rounded-md border border-border px-3 py-2 text-[13px]">
      {run.commit ? (
        <>
          Committed <Mono>{run.commit}</Mono>
          {run.pushed ? ' and pushed' : ', not pushed'}
          {run.pushed && run.url ? (
            <>
              {' '}
              to <Raw>{run.url}</Raw>
            </>
          ) : run.pushed && run.url_hidden ? ' (the address has characters that cannot be shown)' : ''}
          .
        </>
      ) : (
        'Done.'
      )}
    </p>
  )
}

function Status({ data, workspace }: { data: RecordsStatus; workspace: Workspace }) {
  const who = (p: string) => <Inline>{displayName(workspace.members, p)}</Inline>
  const { repo, pending, last, last_noted: noted, auto, remote } = data
  return (
    <dl className="divide-y divide-border">
      <Row label="Repository">
        {repo ? (
          <>
            {repo.state ?? 'unknown'}
            {repo.problem ? (
              <>
                : <Raw>{repo.problem}</Raw>
              </>
            ) : repo.problem_hidden ? ': the problem text has characters that cannot be shown' : ''}
          </>
        ) : (
          'none'
        )}
      </Row>
      <Row label="Waiting to commit">
        {pending ? (
          <>
            {plural(pending.events, 'event')} in {plural(pending.tickets.length, 'ticket')}
            {pending.more ? ' or more' : ''}, plus {plural(pending.workspace_events, 'workspace event')}
            {pending.tickets.length > 0 && (
              <ul className="mt-1 flex flex-wrap gap-x-3 text-[12px] text-text-muted">
                {pending.tickets.map((t) => (
                  <li key={t.key}>
                    <Mono>{t.key}</Mono> {t.events}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          'unknown'
        )}
      </Row>
      <Row label="Last commit">
        {last ? (
          <>
            <Mono>{last.commit}</Mono> at {fmtDateTime(last.at)} by {who(last.by)}, {last.pushed_at ? `pushed ${fmtDateTime(last.pushed_at)}` : 'not pushed'}
          </>
        ) : (
          'none noted'
        )}
        {noted && (
          <span className="block text-[12px] text-text-muted">
            The last note in this workspace is from {who(noted.by)} at {fmtDateTime(noted.at)}. This is a note, not a live look at git.
          </span>
        )}
      </Row>
      <Row label="Automatic commits">
        {auto ? (auto.on ? `on, every ${plural(auto.every_min, 'minute')}${auto.push ? ', with push' : ', without push'}` : 'off') : 'not set up'}
      </Row>
      <Row label="Remote">
        {remote ? (
          <div className="space-y-1">
            {remote.url !== null ? <Raw>{remote.url}</Raw> : remote.url_hidden ? 'its address has characters that cannot be shown' : 'none'}
            {remote.private === true && remote.url !== null && <PrivateRemoteLine url={remote.url} />}
            {remote.export === 'blocked_convenience' && (
              <p data-testid="records-export-blocked" className="text-[12px] text-warning">
                A push is blocked: convenience signing leaves keys other readers could use, and this remote is not marked private.
              </p>
            )}
          </div>
        ) : (
          'none'
        )}
      </Row>
    </dl>
  )
}

export function HostRecords({ workspace }: { workspace: Workspace }) {
  const role = useViewerRole(workspace)
  const qc = useQueryClient()
  const allowed = atLeast(role, 'maintainer')
  const status = useQuery({ ...queries.records(workspace.id), enabled: allowed })
  const [confirmPush, setConfirmPush] = useState(false)
  const [result, setResult] = useState<RecordsRun | null>(null)
  const run = useMutation({
    mutationFn: (what: 'commit' | 'push' | 'auto-off') => (what === 'auto-off' ? api.recordsAutoOff(workspace.id) : api.recordsCommit(workspace.id, what === 'push')),
    onMutate: () => setResult(null),
    onSuccess: (r) => {
      setResult(r)
      void qc.invalidateQueries({ queryKey: ['records', workspace.id] })
    },
    onError: () => void qc.invalidateQueries({ queryKey: ['records', workspace.id] }),
  })
  if (role === undefined) return <p role="status" className="text-[13px] text-text-muted">Reading your role…</p>
  if (!allowed) {
    return (
      <div className="space-y-4">
        <h2 className="text-base font-semibold">Records</h2>
        <p data-testid="records-role" className="text-[13px] text-text-muted">
          Records are for owners and maintainers: a push publishes every ticket, restricted ones included.
        </p>
      </div>
    )
  }
  const data = status.data
  const refused = status.error instanceof ApiError ? status.error : null
  if (status.isError && !refused) return <LoadFailed what="records" onRetry={() => void status.refetch()} />
  const url = data?.remote?.url ?? null
  const pushConfigured = !!data && (!!data.remote?.url || data.auto?.push === true || !!data.remote?.url_hidden)
  const blocked = data?.remote?.export === 'blocked_convenience'
  const canPush = !!data?.remote?.url && !blocked
  const busy = run.isPending

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Records</h2>
      <p className="text-[13px] text-text-muted">The workspace's tickets and events as a git repository you own. Committing is local; pushing sends them to the remote below.</p>

      {refused ? (
        <p role="alert" data-testid="records-refused" className="rounded-md border border-border px-3 py-2 text-[13px] text-text-muted">
          The host did not give the records status: <Mono>{refused.code}</Mono>.
        </p>
      ) : !data ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the records status…
        </p>
      ) : !data.available ? (
        <p data-testid="records-unavailable" className="text-[13px] text-text-muted">
          This host does not serve records, so there is nothing to show or run here.
        </p>
      ) : (
        <>
          <Section title="Status">
            {data.configured === false && (
              <div data-testid="records-not-configured" className="mb-3 space-y-1 text-[13px] text-text-muted">
                <p>No commit has been noted for you in this workspace yet. The repository is made once, in your terminal:</p>
                <code className="block rounded bg-surface-3 px-2 py-1 font-mono text-[12px] text-text">orch records init</code>
              </div>
            )}
            <Status data={data} workspace={workspace} />
          </Section>

          <Section title="Run now">
              <div className="space-y-3">
                {pushConfigured && <ExportWarning url={url} />}
                <div className="flex flex-wrap gap-2">
                  <Button disabled={busy} onClick={() => run.mutate('commit')}>
                    Commit now
                  </Button>
                  {canPush && (
                    <Button variant="outline" disabled={busy} onClick={() => setConfirmPush(true)}>
                      Commit and push
                    </Button>
                  )}
                  {data.auto?.on && (
                    <Button variant="outline" disabled={busy} onClick={() => run.mutate('auto-off')}>
                      Turn automatic commits off
                    </Button>
                  )}
                </div>
                {busy && (
                  <p role="status" className="text-[12px] text-text-muted">
                    Running… this stops after 60 seconds.
                  </p>
                )}
                {run.error instanceof ApiError && <RunFailure error={run.error} />}
                {run.error && !(run.error instanceof ApiError) && <p role="alert" className="text-[13px] text-danger">The request failed. Nothing is known about the outcome; reload the status.</p>}
                {result && <RunResult run={result} />}
              </div>
          </Section>

          <Section title="In your terminal">
            <div className="space-y-2">
              <p className="text-[13px] text-text-muted">These change who can read your records or what runs on your machine, so the dashboard has no button for them. Run them yourself.</p>
              <ul className="space-y-2">
                <Command what="Mark the remote as one only you read (it asks you first).">{TERMINAL_COMMANDS.remotePrivate}</Command>
                <Command what="Commit on a timer, locally.">{TERMINAL_COMMANDS.autoOn}</Command>
                <Command what="Commit and push on a timer.">{TERMINAL_COMMANDS.autoOnPush}</Command>
                <Command what="Create the remote branch the first time.">{TERMINAL_COMMANDS.pushInitial}</Command>
              </ul>
            </div>
          </Section>
        </>
      )}

      {confirmPush && (
        <Dialog open onOpenChange={(o) => !o && setConfirmPush(false)}>
          <DialogContent className="max-w-lg gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Commit and push records</DialogTitle>
              <DialogDescription>Core runs the records tool on this machine. No signature is needed.</DialogDescription>
            </DialogHeader>
            <ExportWarning url={url} />
            {data?.remote?.private === true && url !== null && <PrivateRemoteLine url={url} />}
            <DialogFooter className="gap-2">
              <Button variant="ghost" onClick={() => setConfirmPush(false)}>
                Cancel
              </Button>
              <Button
                onClick={() => {
                  setConfirmPush(false)
                  run.mutate('push')
                }}
              >
                Commit and push
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}
