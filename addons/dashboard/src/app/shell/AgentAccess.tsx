import { KeyRound } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { runnerQuery } from '@/api/agents'
import { accessReminder } from '@/api/grants'
import { queries } from '@/api/queries'
import { hasFeature } from '@/api/features'
import { fmtClock, fmtExact, fmtSlot, nowMs } from '@/lib/time'
import { useMe } from '@/app/useMe'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { HandoffDialog } from '@/components/sign/HandoffDialog'
import { RunnerGrantCard } from '@/app/terminal/real/StartAgentDialog'

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
  const signed = grants.data?.find(g => g.id === live?.id && g.person === me.data?.person)
  // The pill names the SOONEST end of the person's own active agent access (runner grant plus listed grants).
  const ends = live ? [...new Set([Date.parse(live.until), ...(grants.data ?? []).filter(g => g.person === me.data?.person && !g.revoked && g.id !== live.id && Date.parse(g.expires_at) > now).map(g => Date.parse(g.expires_at))])].sort((a, b) => a - b) : []
  const soonest = live && ends.length ? new Date(ends[0]).toISOString() : live?.until
  const more = ends.length - 1
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
  return <div className="agent-access flex min-w-0 items-center gap-2 text-xs">
    <div className="flex min-w-0 shrink-0 items-center gap-1 rounded-full border border-border bg-surface px-3 py-1" role="group" aria-label="Agent access">
      {idle && !partial ? <><span role="status">Agents are off</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Let agents work for you…</Button></> : partial ? <span role="status">Stopped {partial.done} of {partial.total}. {partial.total - partial.done} still active — <Button size="xs" variant="ghost" onClick={() => setAskStop(true)}>Stop again</Button></span> : stopped ? <><span role="status">Agents stopped</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Start again</Button></> : live ? <>
        <KeyRound className="size-3.5 shrink-0" aria-hidden /><span className="whitespace-nowrap"><span className="agent-access-detail">Agents can work for you until </span><time dateTime={soonest} title={fmtExact(soonest!)}>{fmtSlot(soonest!)}</time>{more > 0 && <span className="text-text-muted"> (+{more} more)</span>}</span>
        <span>·</span><Button size="xs" variant="ghost" aria-label="Extend agent access" onClick={() => setExtend(true)}>Extend</Button>
      </> : <span>Checking agent access…</span>}
      {!partial && live && <><span>·</span><Button size="xs" variant="ghost" disabled={!hasFeature('do:grant.revoke', ws)} onClick={() => setAskStop(true)}>Stop</Button></>}
    </div>
    {live && signed && accessReminder({ issued_at: signed.issued_at, until: live.until }, now) && <div role="status" className="agent-access-reminder flex min-w-0 items-center gap-1 whitespace-nowrap text-warning">Agent access ends at {fmtClock(live.until)} · <Button size="xs" variant="ghost" aria-label="Extend agent access from reminder" onClick={() => setExtend(true)}>Extend</Button></div>}
    {extend && <Dialog open onOpenChange={open => { if (!open) setExtend(false) }}><DialogContent className="max-h-[90vh] overflow-y-auto"><DialogHeader><DialogTitle>{live ? 'Extend agent access' : stopped ? 'Restore agent access' : 'Let agents work for you'}</DialogTitle><DialogDescription>{live ? 'Choose how long agents may keep working for you, then review and sign.' : stopped ? 'Choose how long agents may work for you again, then review and sign.' : 'Choose how long agents may work for you, then review and sign.'}</DialogDescription></DialogHeader>
      <RunnerGrantCard ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={null} extend={live ?? undefined} onChanged={refresh} onOutcome={outcome} />
    </DialogContent></Dialog>}
    {askStop && <Dialog open onOpenChange={open => { if (!open) setAskStop(false) }}><DialogContent><DialogHeader><DialogTitle>Stop agents working for you?</DialogTitle><DialogDescription>This ends your runner access and all your other listed active agent access. Review and sign each stop separately. An agent that is already running can no longer save changes, but it keeps running until you press Stop on it in the agents dock.</DialogDescription></DialogHeader>
      {error && <p role="alert">{error}</p>}<Button variant="ghost" onClick={() => setAskStop(false)}>Cancel</Button><Button disabled={grants.isFetching} onClick={() => void stop()}>Stop agent access</Button>
    </DialogContent></Dialog>}
    {queue[done] && <HandoffDialog key={queue[done].id} ws={ws} title="Stop agent access" action={{ action: 'grant_revoke', grant: queue[done].id }} autoStart summary={<><p role="status">Stopping access {done + 1} of {queue.length}. {done} stopped.</p><p>This ends the agent access for {me.data?.name ?? 'you'}{queue[done].until ? <>, which runs until <time dateTime={queue[done].until}>{fmtSlot(queue[done].until!)}</time></> : ''}.</p><details className="text-text-muted"><summary className="cursor-pointer">Technical details</summary><p>Access id: {queue[done].id}</p></details></>} onSettled={phase => {
      if (phase !== 'done') {
        if (phase !== 'unknown' && phase !== 'attempt_open') interrupted()
        return
      }
      if (done + 1 === queue.length) { setQueue([]); setAttempted(false); setStoppedHere(true) } else setDone(done + 1)
      refresh()
    }} onInterrupted={interrupted} onClose={interrupted} />}
  </div>
}
