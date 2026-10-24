// Records on a real host (M6, host contract 12.8 and 12.11): the status the host gives owners and maintainers, the
// actions (commit [--push] and auto off) plus a status check, each a fixed child of the server; and, as exact commands
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
    <div className="grid grid-cols-[minmax(0,120px)_minmax(0,1fr)] items-start gap-3 py-1.5 text-[13px]">
      <dt className="text-text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  )
}

/** The current destination uses §12.11 verbatim; historical status remains labelled. */
export function ExportWarning({ url, current = false, hostOnly = false }: { url: string | null; current?: boolean; hostOnly?: boolean }) {
  // hostOnly: core shows a non-owner only the host of the current destination (or "local"), never the full address.
  return (
    <p data-testid="records-export-warning" className="text-[13px] text-warning">
      {EXPORT_WARNING.before}
      {current && url !== null && hostOnly ? <>the remote configured in the records repository (on <Raw>{url}</Raw>; only owners see the full address)</>
        : current && url !== null ? <Raw>{url}</Raw>
        : <>the remote configured in the records repository (last known: {url === null ? 'cannot be shown here' : <Raw>{url}</Raw>})</>}
      {EXPORT_WARNING.after}
    </p>
  )
}

export function PrivateRemoteLine({ url }: { url: string }) {
  return (
    <p data-testid="records-private-line" className="text-[13px] text-text-muted">
      Last noted as private: <Raw>{url}</Raw>.
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
        <span className="font-medium text-danger">{error.code === 'records.not_dedicated' ? "The records folder isn't set up yet." : 'Not done.'}</span>
      </p>
      <details><summary className="cursor-pointer">Details</summary><Mono>{error.code}</Mono></details>
      {error.code !== 'records.not_dedicated' && !error.message_hidden && error.message !== error.code && (
        <div className="text-text-muted" aria-label="Output of the records tool">
          <p className="font-medium">Output of the records tool</p>
          <p><Inline>{error.message}</Inline></p>
        </div>
      )}
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
          'nothing noted yet'
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
            {remote.url !== null ? <>Last known destination: <Raw>{remote.url}</Raw></> : remote.url_hidden ? 'its address has characters that cannot be shown' : 'none'}
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

export function RecordsBlock({ workspace }: { workspace: Workspace }) {
  const role = useViewerRole(workspace)
  const qc = useQueryClient()
  const allowed = atLeast(role, 'maintainer')
  const status = useQuery({ ...queries.records(workspace.id), enabled: allowed })
  const [confirmPush, setConfirmPush] = useState(false)
  const current = useMutation({ mutationFn: () => api.getRecordsCurrent(workspace.id) })
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
  if (!allowed) return <p data-testid="records-role" className="text-[13px] text-text-muted">Records are for owners and maintainers: a push publishes every ticket, restricted ones included.</p>
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
      <h4 className="font-semibold">Records</h4>

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
          <p data-testid="records-summary" className="text-[13px]">
            {data.configured === false || !data.repo
              ? 'Your tickets and history are not saved yet.'
              : <>{data.auto?.on ? 'Your tickets and history are saved automatically on this computer.' : 'Your tickets and history are saved only when you press Save now.'} {data.last ? `Last saved ${fmtDateTime(data.last.at)}.` : 'Nothing saved yet.'}</>}
            {data.pending && data.pending.events > 0 && ` ${plural(data.pending.events, 'change')} are waiting to be saved.`}
          </p>
          <div className="flex flex-wrap gap-2">
            {(data.configured === false || !data.repo) && <Button disabled={busy} onClick={() => run.mutate('commit')}>Set up</Button>}
            <Button disabled={busy} onClick={() => run.mutate('commit')}>Save now</Button>
            {run.error instanceof ApiError && run.error.code === 'records.not_dedicated' && <Button disabled={busy} onClick={() => run.mutate('commit')}>Set up records</Button>}
          </div>
          {busy && (
            <p role="status" className="text-[12px] text-text-muted">
              Running… this stops after 60 seconds.
            </p>
          )}
          {run.error instanceof ApiError && <RunFailure error={run.error} />}
          {run.error && !(run.error instanceof ApiError) && <p role="alert" className="text-[13px] text-danger">The request failed. Nothing is known about the outcome; reload the status.</p>}
          {result && <RunResult run={result} />}
          <details className="space-y-3">
            <summary className="cursor-pointer text-text-muted">For advanced users</summary>
            <Section title="Status">
              {/* `configured` means "the workspace records repository exists" (core #454: ok, index_differs and unsafe count, even before a commit). */}
              {data.configured === false && (
                <p data-testid="records-not-configured" className="mb-3 text-[13px] text-text-muted">
                  No records repository yet; the first commit creates it.
                </p>
              )}
              <Status data={data} workspace={workspace} />
            </Section>

            <Section title="Run now">
                <div className="space-y-3">
                  {pushConfigured && <ExportWarning url={url} />}
                  <div className="flex flex-wrap gap-2">
                    {canPush && (
                      <Button variant="outline" disabled={busy} onClick={() => { current.reset(); current.mutate(); setConfirmPush(true) }}>
                        Commit and push
                      </Button>
                    )}
                    {data.auto?.on && (
                      <Button variant="outline" disabled={busy} onClick={() => run.mutate('auto-off')}>
                        Turn automatic commits off
                      </Button>
                    )}
                  </div>
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
          </details>
        </>
      )}

      {confirmPush && (
        <Dialog open onOpenChange={(o) => !o && setConfirmPush(false)}>
          <DialogContent className="max-w-lg gap-4 border-border bg-surface">
            <DialogHeader>
              <DialogTitle>Commit and push records</DialogTitle>
              <DialogDescription>Core runs the records tool on this machine. No signature is needed.</DialogDescription>
            </DialogHeader>
            {current.isPending ? <p role="status">Checking where push goes…</p> : (() => {
              const remote = current.data?.remote
              const checked = !current.isError && remote?.current_url != null && !remote.current_url_hidden
              return <div className="space-y-2">
                <ExportWarning url={checked ? remote.current_url! : url} current={checked} hostOnly={role !== 'owner'} />
                {/* The private mark is the owner's word about the full address: never shown against a host-only view. */}
                {checked && role === 'owner' && remote.current_private === true && <p data-testid="records-current-private" className="text-[13px] text-text-muted">{PRIVATE_REMOTE.before}<Raw>{remote.current_url!}</Raw>{PRIVATE_REMOTE.after}</p>}
                {checked && remote.matches_last === false && <p data-testid="records-push-mismatch" className="text-[13px] text-warning">This is not where the last push went (last: {remote.url !== null ? <Raw>{remote.url}</Raw> : 'cannot be shown here'}).</p>}
                {!checked && <p role="alert" className="text-[13px] text-warning">Couldn't check the current destination: <Mono>{current.error instanceof ApiError ? current.error.code : current.isError ? 'records.child_unavailable' : 'records.current_url_unavailable'}</Mono>.</p>}
              </div>
            })()}

            <DialogFooter className="gap-2">
              <Button variant="ghost" onClick={() => setConfirmPush(false)}>
                Cancel
              </Button>
              <Button
                disabled={current.isPending}
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
