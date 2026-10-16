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
export function AgentAccess({ ws }: { ws: string }) {
  const me = useMe(ws)
  const runner = useQuery({ ...runnerQuery(ws), refetchInterval: 15_000 })
  const grants = useQuery(queries.hostGrants(ws, true))
  const qc = useQueryClient()
  const [now, setNow] = useState(nowMs)
  const [extend, setExtend] = useState(false)
  const [attempted, setAttempted] = useState(false)
  const [askStop, setAskStop] = useState(false)
  const [queue, setQueue] = useState<string[]>([])
  const [done, setDone] = useState(0)
  const [partial, setPartial] = useState<{ done: number; total: number } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => { const timer = setInterval(() => setNow(nowMs()), 30_000); return () => clearInterval(timer) }, [])
  const grant = runner.data?.runner?.grant
  const live = grant && Date.parse(grant.until) > now ? grant : null
  const stopped = attempted && runner.isSuccess && !runner.isFetching && !live
  const signed = grants.data?.find(g => g.id === live?.id && g.person === me.data?.person)
  const refresh = () => { void runner.refetch(); void qc.invalidateQueries({ queryKey: ['settings-grants', ws] }) }
  // The API exposes deposit status, but no deposit retry. Start again opens a fresh signing flow.
  const outcome = () => { setAttempted(true); refresh() }
  const stop = async () => {
    setError('')
    const [result, currentRunner] = await Promise.all([grants.refetch(), runner.refetch()])
    if (result.isError || currentRunner.isError || !me.data?.person) { setError('Could not read your access. Try again.'); return }
    const own = result.data?.filter(g => g.person === me.data.person && !g.revoked && Date.parse(g.expires_at) > nowMs()).map(g => g.id) ?? []
    const current = currentRunner.data?.runner?.grant
    setPartial(null)
    setQueue([...new Set([...(current && Date.parse(current.until) > nowMs() ? [current.id] : []), ...own])])
    setDone(0)
    setAskStop(false)
  }
  const interrupted = () => {
    setPartial({ done, total: queue.length })
    setQueue([])
    refresh()
  }
  if (!live && !stopped && !extend && !askStop && !partial && queue.length === 0) return null
  return <div className="agent-access flex min-w-0 items-center gap-2 text-xs">
    <div className="flex min-w-0 shrink-0 items-center gap-1 rounded-full border border-border bg-surface px-3 py-1" role="group" aria-label="Agent access">
      {partial ? <span role="status">Stopped {partial.done} of {partial.total}. {partial.total - partial.done} still active — <Button size="xs" variant="ghost" onClick={() => setAskStop(true)}>Stop again</Button></span> : stopped ? <><span role="status">Agents stopped</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Start again</Button><span className="text-text-muted">Signs new agent access</span></> : live ? <>
        <KeyRound className="size-3.5 shrink-0" aria-hidden /><span className="whitespace-nowrap"><span className="agent-access-detail">Agents can work for you until </span><time dateTime={live.until} title={fmtExact(live.until)}>{fmtSlot(live.until)}</time></span>
        <span>·</span><Button size="xs" variant="ghost" aria-label="Extend agent access" onClick={() => setExtend(true)}>Extend</Button>
      </> : <span>Checking agent access…</span>}
      {!partial && (live || stopped) && <><span>·</span><Button size="xs" variant="ghost" disabled={!hasFeature('do:grant.revoke', ws)} onClick={() => setAskStop(true)}>Stop</Button></>}
    </div>
    {live && signed && accessReminder({ issued_at: signed.issued_at, until: live.until }, now) && <div role="status" className="agent-access-reminder flex min-w-0 items-center gap-1 whitespace-nowrap text-warning">Agent access ends at {fmtClock(live.until)} · <Button size="xs" variant="ghost" aria-label="Extend agent access from reminder" onClick={() => setExtend(true)}>Extend</Button></div>}
    {extend && <Dialog open onOpenChange={open => { if (!open) setExtend(false) }}><DialogContent className="max-h-[90vh] overflow-y-auto"><DialogHeader><DialogTitle>{stopped ? 'Restore agent access' : 'Extend agent access'}</DialogTitle><DialogDescription>Choose how long agents may keep working for you, then review and sign. Agents that are running now stop; new agents use the extended access.</DialogDescription></DialogHeader>
      <RunnerGrantCard ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={null} extend={live ?? undefined} onChanged={refresh} onOutcome={outcome} />
    </DialogContent></Dialog>}
    {askStop && <Dialog open onOpenChange={open => { if (!open) setAskStop(false) }}><DialogContent><DialogHeader><DialogTitle>Stop agents working for you?</DialogTitle><DialogDescription>This ends your runner access and all your other listed active agent access. Review and sign each stop separately.</DialogDescription></DialogHeader>
      {error && <p role="alert">{error}</p>}<Button variant="ghost" onClick={() => setAskStop(false)}>Cancel</Button><Button disabled={grants.isFetching} onClick={() => void stop()}>Stop agent access</Button>
    </DialogContent></Dialog>}
    {queue[done] && <HandoffDialog key={queue[done]} ws={ws} title="Stop agent access" action={{ action: 'grant_revoke', grant: queue[done] }} autoStart summary={<p role="status">Stopping access {done + 1} of {queue.length}. {done} stopped.</p>} onSettled={phase => {
      if (phase !== 'done') {
        if (phase !== 'unknown' && phase !== 'attempt_open') interrupted()
        return
      }
      if (done + 1 === queue.length) { setQueue([]); setAttempted(false) } else setDone(done + 1)
      refresh()
    }} onInterrupted={interrupted} onClose={interrupted} />}
  </div>
}
