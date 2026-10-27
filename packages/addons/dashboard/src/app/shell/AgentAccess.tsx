import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { runnerQuery } from '@/api/agents'
import { accessLine } from '@/api/grants'
import { queries } from '@/api/queries'
import { hasFeature } from '@/api/features'
import { fmtClock, fmtExact, fmtSlot, fmtSpan, nowMs } from '@/lib/time'
import { useMe } from '@/app/useMe'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { SignPopover } from '@/components/sign/SignPopover'
import { RunnerGrantCard } from '@/app/terminal/real/StartAgentDialog'

/** How long is left, in the words the pill uses: "3 days left", "5 hours left", "20 min left". */
export function timeLeft(ms: number): string {
  if (ms >= 36 * 3_600_000) return `${Math.round(ms / 86_400_000)} days left`
  if (ms >= 3_600_000 * 1.5) return `${Math.round(ms / 3_600_000)} hours left`
  return `${fmtSpan(ms)} left`
}

/** Runner state is authoritative for access on this machine; grant.list supplies signed start times and terminal grants. */
export function AgentAccess({ ws, previewStopped = false }: { ws: string; /** Dev layout preview only: start in the stopped state. */ previewStopped?: boolean }) {
  const me = useMe(ws)
  const runner = useQuery({ ...runnerQuery(ws), refetchInterval: 15_000 })
  const grants = useQuery(queries.hostGrants(ws, true))
  const qc = useQueryClient()
  const [now, setNow] = useState(nowMs)
  const [extend, setExtend] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [stoppedHere, setStoppedHere] = useState(previewStopped)
  const [askStop, setAskStop] = useState(false)
  const [queue, setQueue] = useState<{ id: string; until?: string }[]>([])
  const [done, setDone] = useState(0)
  const [partial, setPartial] = useState<{ done: number; total: number } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { const timer = setInterval(() => setNow(nowMs()), 30_000); return () => clearInterval(timer) }, [])
  const grant = runner.data?.runner?.grant
  const live = grant && Date.parse(grant.until) > now ? grant : null
  const stopped = (attempted || stoppedHere) && runner.isSuccess && !runner.isFetching && !live
  const line = accessLine(runner.data?.runner?.grant, now)
  const refresh = () => { void runner.refetch(); void qc.invalidateQueries({ queryKey: ['settings-grants', ws] }); void qc.invalidateQueries({ queryKey: ['runs', ws] }) }
  // The API exposes deposit status, but no deposit retry. Start again opens a fresh signing flow.
  const outcome = () => { setAttempted(true); refresh() }
  const stop = async () => {
    setError('')
    const [result, currentRunner] = await Promise.all([grants.refetch(), runner.refetch()])
    if (result.isError || currentRunner.isError || !me.data?.person) { setError('Could not read your access. Try again.'); return }
    const own = result.data?.filter(g => g.person === me.data.person && !g.revoked && Date.parse(g.expires_at) > nowMs()).map(g => ({ id: g.id, until: g.expires_at })) ?? []
    const current = currentRunner.data?.runner?.grant
    setPartial(null)
    const all = [...(current && Date.parse(current.until) > nowMs() ? [{ id: current.id, until: current.until }] : []), ...own]
    setQueue(all.filter((g, i) => all.findIndex(o => o.id === g.id) === i))
    setDone(0)
    setAskStop(false)
  }
  const interrupted = () => {
    // Nothing stopped yet (cancelled before the first signature): restore the normal pill, no failure state.
    setPartial(done > 0 ? { done, total: queue.length } : null)
    setQueue([])
    refresh()
  }
  // Nothing active and not just stopped: offer a way in whenever the runner answered (a reload after Stop, or no grant ever).
  const idle = !live && !stopped && runner.isSuccess && !runner.isFetching
  if (!live && !stopped && !idle && !extend && !askStop && !partial && queue.length === 0) return null
  // Enough access left: the pill stays hidden (Stop is on Settings > Grants) unless a stop or extend flow is under way.
  if (line.kind === 'ok' && !extend && !askStop && !partial && queue.length === 0) return null
  const pill = 'flex min-h-11 min-w-0 shrink-0 items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-text transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-9'
  return <div className="agent-access flex shrink-0 min-w-0 items-center gap-2 text-xs">
    {partial ? <div className={pill} role="group" aria-label="Agent access">
      <span role="status">Stopped {partial.done} of {partial.total}. {partial.total - partial.done} still active — <Button size="xs" variant="ghost" onClick={() => setAskStop(true)}>Stop again</Button></span>
    </div> : line.kind === 'ending' ? <div className={`${pill} whitespace-nowrap border-warning/40 bg-warning/5 text-warning`} role="group" aria-label="Agent access">
      <span aria-hidden className="size-2 shrink-0 rounded-full bg-warning" />
      <span>Agent access ends at {fmtClock(line.until)}</span>
      <time className="sr-only" dateTime={line.until} title={fmtExact(line.until)}>{fmtSlot(line.until)}</time>
      <Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Allow agents</Button>
      <Button size="xs" variant="ghost" className="text-text-muted" disabled={!hasFeature('do:grant.revoke', ws)} onClick={() => setAskStop(true)}>Stop</Button>
    </div> : line.kind === 'missing' ? <div className={`${pill}${live || idle || stopped ? ' whitespace-nowrap' : ''}`} role="group" aria-label="Agent access">
      {live || idle || stopped ? <><span role="status">Agents can't start yet</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Allow agents</Button></> : <span>Checking agent access…</span>}
    </div> : null}
    {extend && <Dialog open onOpenChange={open => { if (!open) setExtend(false) }}><DialogContent className="max-h-[90vh] overflow-y-auto"><DialogHeader><DialogTitle>Allow agents</DialogTitle><DialogDescription>Agents may work for you for the time you choose, counted from now.{live ? ' It replaces your current access.' : ''}</DialogDescription></DialogHeader>
      <RunnerGrantCard sheet ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={null} extend={live ?? undefined} onChanged={refresh} onOutcome={outcome} onConfirmed={() => setExtend(false)} />
      {live && <details className="text-xs text-text-muted"><summary className="cursor-pointer">Technical details</summary><p>Access id: {live.id}</p></details>}
    </DialogContent></Dialog>}
    {askStop && <Dialog open onOpenChange={open => { if (!open) setAskStop(false) }}><DialogContent><DialogHeader><DialogTitle>Stop agents working for you?</DialogTitle><DialogDescription>This ends your runner access and all your other listed active agent access. Review and sign each stop separately. An agent that is already running can no longer save changes, but it keeps running until you press Stop on it in the agents dock.</DialogDescription></DialogHeader>
      {error && <p role="alert">{error}</p>}<Button variant="ghost" onClick={() => setAskStop(false)}>Cancel</Button><Button disabled={grants.isFetching} onClick={() => void stop()}>Stop agent access</Button>
    </DialogContent></Dialog>}
    {queue[done] && <SignPopover key={queue[done].id} ws={ws} title="Stop agent access" action={{ action: 'grant_revoke', grant: queue[done].id }} autoStart summary={<><p role="status">Stopping access {done + 1} of {queue.length}. {done} stopped.</p><p>This ends the agent access for {me.data?.name ?? 'you'}{queue[done].until ? <>, which runs until <time dateTime={queue[done].until}>{fmtSlot(queue[done].until!)}</time></> : ''}.</p><details className="text-text-muted"><summary className="cursor-pointer">Technical details</summary><p>Access id: {queue[done].id}</p></details></>} onSettled={phase => {
      if (phase !== 'done') {
        if (phase !== 'unknown' && phase !== 'attempt_open') interrupted()
        return
      }
      if (done + 1 === queue.length) { setQueue([]); setAttempted(false); setStoppedHere(true) } else setDone(done + 1)
      refresh()
    }} onInterrupted={interrupted} onClose={interrupted} />}
  </div>
}
