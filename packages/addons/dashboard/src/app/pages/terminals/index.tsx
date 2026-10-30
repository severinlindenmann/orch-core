// Terminals on a real host (add-on): every terminal and agent session of yours in one list. It reads the same `runs`
// list as the dock and uses the dock's own routes (open = select it in the dock, stop, New terminal with its preview);
// nothing here talks to the host differently. The bottom dock keeps working as before.
import { RunnerSwitch } from '@/components/RunnerSwitch'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from '@tanstack/react-router'
import { Plus } from 'lucide-react'
import { useState } from 'react'
import { hasFeature } from '@/api/features'
import { queries } from '@/api/queries'
import { agentFinished } from '@/app/pages/ticket/progress'
import { cleanupDoubt, exitText, runQueries, runsApi, type Run } from '@/api/runs'
import { ApiError } from '@/api/types'
import { LoadFailed } from '@/components/LoadFailed'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { fmtDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'
import { PreviewDialog } from '@/app/terminal/real/PreviewDialog'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { useOpenTerminal } from '@/app/terminal/real/useOpenTerminal'
import { runName } from '@/app/terminal/real/runName'
import { openDockOn } from '@/app/terminal/dock/request'
import { useWorkspace } from '@/app/workspace'

const live = (r: Run) => r.state === 'running' || r.state === 'starting'
const kindText = (r: Run) => (r.kind === 'agent' ? 'Agent' : 'Terminal')
/** Who may type right now: the write lease is held by one tab or device; every other viewer reads. */
const controlText = (r: Run) => (!live(r) ? '' : r.writer ? 'Controlled' : 'Read only')

function Row({ r, onStop, doubt, finished }: { r: Run; onStop: (r: Run) => void; doubt: boolean; finished: boolean }) {
  const name = r.label ? <Inline>{r.label}</Inline> : runName(r)
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3 text-[13px]" data-run={r.run} data-state={live(r) ? 'live' : 'ended'} data-finished={finished ? 'true' : undefined}>
      <span aria-hidden="true" className={cn('size-2 shrink-0 rounded-full', live(r) && !finished ? 'bg-success' : 'bg-text-faint')} />
      <span className="min-w-0 flex-1 basis-48">
        <span className="block truncate font-medium">{name}</span>
        <span className="block text-[12px] text-text-muted">
          {kindText(r)} · started {fmtDateTime(r.started_at)}{finished ? ' · Ticket is done' : ''}
          {doubt ? ' · some processes may still be running' : ''}
        </span>
        {!live(r) && exitText(r) && <span className="block text-[12px] text-text-muted" data-testid="run-exit">{exitText(r)}</span>}
      </span>
      {r.ticket && (
        <Link to="/ticket/$key" params={{ key: r.ticket }} className="font-mono text-[12px] text-brand hover:underline">
          {r.ticket}
        </Link>
      )}
      <span className={cn('rounded-full border px-2 py-px text-[11px]', live(r) && !finished ? 'border-success/50 text-success' : 'border-border text-text-muted')}>{finished ? 'Finished' : live(r) ? 'Live' : 'Ended'}</span>
      <span className="w-24 text-[12px] text-text-muted">{finished ? '' : controlText(r)}</span>
      <span className="flex gap-1">
        <Button size="sm" variant="outline" className="h-7 text-[12px]" aria-label={`Open ${runName(r)}`} onClick={() => openDockOn(r.run)}>
          Open
        </Button>
        {live(r) && (
          <Button size="sm" variant="ghost" className="h-7 text-[12px]" aria-label={`${finished ? 'Close' : 'Stop'} ${runName(r)}`} onClick={() => onStop(r)}>
            {finished ? 'Close' : 'Stop'}
          </Button>
        )}
      </span>
    </li>
  )
}

export function TerminalsPage() {
  usePageHeader('Terminals')
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const qc = useQueryClient()
  const served = !!ws && hasFeature('terminals_page', ws)
  const list = useQuery({ ...runQueries.list(ws ?? ''), enabled: served })
  // Same tickets list the rest of the dashboard reads; while it loads, a row keeps showing what the process does.
  const tickets = useQuery({ ...queries.ticketsAll(ws ?? ''), enabled: served })
  const statusOf = new Map((tickets.data ?? []).map((t) => [t.key, t.status]))
  const [error, setError] = useState('')
  const [doubt, setDoubt] = useState<Record<string, boolean>>({})
  const terminal = useOpenTerminal(ws ?? '')
  const [starting, setStarting] = useState(false)
  if (!workspace || !ws) return null
  const agents = hasFeature('agent_start', ws)
  if (!served) {
    return (
      <div className="max-w-2xl space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Terminals</h1>
        <p data-testid="terminals-unavailable" className="text-[13px] text-text-muted">
          Terminals are switched off in this workspace, so there is nothing to list here. Switch them on in Settings, under Add-ons.
        </p>
      </div>
    )
  }
  if (list.isError) return <LoadFailed what="terminals" onRetry={() => void list.refetch()} />
  const runs = (list.data?.runs ?? []).filter((r) => r.kind === 'shell' || r.kind === 'agent')
  const off = list.data?.runner && (!list.data.runner.enabled || !list.data.runner.terminals)
  const ask = () => terminal.ask({})
  const stop = async (r: Run) => {
    setError('')
    try {
      const res = await runsApi.stop(ws, r.run)
      if (cleanupDoubt(res.cleanup)) setDoubt((d) => ({ ...d, [r.run]: true }))
      void qc.invalidateQueries({ queryKey: ['runs', ws] })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'The terminal could not be stopped.')
    }
  }
  return (
    <div className="w-full max-w-4xl space-y-4" data-terminals-page>
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold tracking-tight">Terminals</h1>
        {/* Run 12a owner feedback: starting an agent is the main thing people come here for; a plain terminal is second. */}
        {agents && (
          <Button size="sm" className="ml-auto" data-testid="terminals-start-agent" onClick={() => setStarting(true)}>
            <Plus /> Start agent
          </Button>
        )}
        <Button size="sm" variant={agents ? 'outline' : 'default'} className={agents ? '' : 'ml-auto'} onClick={() => void ask()}>
          <Plus /> New terminal
        </Button>
      </div>
      <p className="text-[13px] text-text-muted">
        Your terminals and the agents you started, in one place. Open one to see it in the terminal panel at the bottom; it opens read only, and you take control there. A terminal runs on this computer, as you.
      </p>
      {off && (
        <div role="status" className="flex flex-wrap items-center gap-2 text-[13px] text-text-muted">
          Terminals are switched off for this workspace on this machine.
          <RunnerSwitch ws={ws} on initialTerminals testId="terminals-runner" variant="secondary" />
        </div>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-danger">
          {error}
        </p>
      )}
      {list.isPending ? (
        <p role="status" className="text-[13px] text-text-muted">
          Reading the terminals…
        </p>
      ) : runs.length === 0 ? (
        <p data-testid="terminals-empty" className="text-[13px] text-text-muted">
          {agents ? 'Nothing is running. Start an agent, or open a terminal with New terminal.' : 'No terminal is open. Start one with New terminal.'}
        </p>
      ) : (
        <ul aria-label="Terminals" className="divide-y divide-border rounded-lg border border-border bg-surface px-3">
          {runs.map((r) => (
            <Row key={r.run} r={r} onStop={(x) => void stop(x)} finished={agentFinished(r, r.ticket ? statusOf.get(r.ticket) : null)} doubt={!!doubt[r.run] || (!!r.exit && cleanupDoubt(r.exit.cleanup))} />
          ))}
        </ul>
      )}
      <PreviewDialog {...terminal} />
      {agents && (
        <StartAgentDialog
          ws={ws}
          open={starting}
          choose
          onClose={() => setStarting(false)}
          onStarted={(run) => {
            setStarting(false)
            void qc.invalidateQueries({ queryKey: ['runs', ws] })
            openDockOn(run.run)
          }}
        />
      )}
    </div>
  )
}
