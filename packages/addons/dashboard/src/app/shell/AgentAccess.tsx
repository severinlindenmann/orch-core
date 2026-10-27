import { ChevronDown } from 'lucide-react'
import { useEffect, useState } from 'react'
import { InfoDrawer } from '@/components/InfoDrawer'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { runnerQuery } from '@/api/agents'
import { accessReminder } from '@/api/grants'
import { queries } from '@/api/queries'
import { hasFeature } from '@/api/features'
import { fmtExact, fmtSlot, fmtSpan, nowMs } from '@/lib/time'
import { useMe } from '@/app/useMe'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
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
  const [open, setOpen] = useState(false)
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
  // Every own, still-valid access, each with what it is and when it ends (the runner on this computer first).
  const rows = live ? [{ id: live.id, what: 'Agents on this computer', until: live.until }, ...(grants.data ?? []).filter(g => g.person === me.data?.person && !g.revoked && g.id !== live.id && Date.parse(g.expires_at) > now).map(g => ({ id: g.id, what: 'Agents started from the terminal', until: g.expires_at }))] : []
  const left = soonest ? Date.parse(soonest) - now : 0
  const ending = !!live && ((signed && accessReminder({ issued_at: signed.issued_at, until: live.until }, now)) || left < 3_600_000)
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
  const pill = 'flex min-h-11 min-w-0 shrink-0 items-center gap-1.5 rounded-full border border-border bg-surface px-2.5 py-1 text-text transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:min-h-9'
  return <div className="agent-access flex shrink-0 min-w-0 items-center gap-2 text-xs">
    {live && !partial ? <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild><button type="button" className={`${pill} ${ending ? 'border-warning/40 bg-warning/5 text-warning' : ''}`}>
        <span aria-hidden className={`size-2 shrink-0 rounded-full ${ending ? 'bg-warning' : 'bg-success'}`} />
        <span className="whitespace-nowrap">{ending ? <>Agent access ends in {fmtSpan(left)}</> : <>Agent access<span className="agent-access-detail"> · {timeLeft(left)}</span></>}</span>
        <time className="sr-only" dateTime={soonest} title={fmtExact(soonest!)}>{fmtSlot(soonest!)}</time>
        <ChevronDown className="size-3.5 shrink-0" aria-hidden />
      </button></PopoverTrigger>
      <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] space-y-4 border-border bg-surface p-4 text-xs leading-relaxed text-text">
        <p className="font-medium text-text">Agents may work for you until <time dateTime={soonest} title={fmtExact(soonest!)}>{fmtExact(soonest!)}</time>.</p>
        <ul aria-label="Your agent access" className="space-y-2 text-text-muted">
          {rows.map(r => <li key={r.id}>{r.what} · until <time dateTime={r.until}>{fmtSlot(r.until)}</time></li>)}
        </ul>
        {more > 0 && <p className="text-text-muted">Extend replaces the access on this computer. The other access keeps its own end time until you stop it.</p>}
        <div className="grid gap-2">
          <Button size="sm" aria-label="Extend agent access" className="min-h-11 w-full" onClick={() => { setOpen(false); setExtend(true) }}>Extend</Button>
          <Button size="sm" variant="outline" className="min-h-11 w-full border-border bg-bg text-text" disabled={!hasFeature('do:grant.revoke', ws)} onClick={() => { setOpen(false); setAskStop(true) }}>Stop</Button>
        </div>
      </PopoverContent>
    </Popover> : <div className={`${pill}${idle && !partial ? ' whitespace-nowrap' : ''}`} role="group" aria-label="Agent access">
      {idle && !partial ? <><span role="status">Agents off</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Turn on</Button></> : partial ? <span role="status">Stopped {partial.done} of {partial.total}. {partial.total - partial.done} still active — <Button size="xs" variant="ghost" onClick={() => setAskStop(true)}>Stop again</Button></span> : stopped ? <><span role="status">Agents stopped</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Start again</Button></> : <span>Checking agent access…</span>}
    </div>}
    {extend && <Dialog open onOpenChange={open => { if (!open) setExtend(false) }}><DialogContent className="max-h-[90vh] overflow-y-auto"><DialogHeader><DialogTitle>{live ? 'Extend agent access' : 'Allow agent access'}</DialogTitle><DialogDescription>{live ? 'Choose how long agents may keep working for you. The time is counted from now and replaces your current access.' : 'Choose how long agents may work for you, counted from now.'}</DialogDescription></DialogHeader>
      <RunnerGrantCard sheet ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={null} extend={live ?? undefined} onChanged={refresh} onOutcome={outcome} onConfirmed={() => setExtend(false)} />
      {live && <InfoDrawer><p>Access id: {live.id}</p></InfoDrawer>}
    </DialogContent></Dialog>}
    {askStop && <Dialog open onOpenChange={open => { if (!open) setAskStop(false) }}><DialogContent><DialogHeader><DialogTitle>Stop agents working for you?</DialogTitle><DialogDescription>This ends your runner access and all your other listed active agent access. Review and sign each stop separately. An agent that is already running can no longer save changes, but it keeps running until you press Stop on it in the agents dock.</DialogDescription></DialogHeader>
      {error && <p role="alert">{error}</p>}<Button variant="ghost" onClick={() => setAskStop(false)}>Cancel</Button><Button disabled={grants.isFetching} onClick={() => void stop()}>Stop agent access</Button>
    </DialogContent></Dialog>}
    {queue[done] && <SignPopover key={queue[done].id} ws={ws} title="Stop agent access" action={{ action: 'grant_revoke', grant: queue[done].id }} autoStart summary={<><p role="status">Stopping access {done + 1} of {queue.length}. {done} stopped.</p><p>This ends the agent access for {me.data?.name ?? 'you'}{queue[done].until ? <>, which runs until <time dateTime={queue[done].until}>{fmtSlot(queue[done].until!)}</time></> : ''}.</p><InfoDrawer><p>Access id: {queue[done].id}</p></InfoDrawer></>} onSettled={phase => {
      if (phase !== 'done') {
        if (phase !== 'unknown' && phase !== 'attempt_open') interrupted()
        return
      }
      if (done + 1 === queue.length) { setQueue([]); setAttempted(false); setStoppedHere(true) } else setDone(done + 1)
      refresh()
    }} onInterrupted={interrupted} onClose={interrupted} />}
  </div>
}
