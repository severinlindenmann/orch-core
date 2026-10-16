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
  const [error, setError] = useState('')
  useEffect(() => { const timer = setInterval(() => setNow(nowMs()), 30_000); return () => clearInterval(timer) }, [])
  const grant = runner.data?.runner?.grant
  const live = grant && Date.parse(grant.until) > now ? grant : null
  const stopped = attempted && runner.isSuccess && !runner.isFetching && !live
  const signed = grants.data?.find(g => g.id === live?.id && g.person === me.data?.person)
  const refresh = () => { void runner.refetch(); void qc.invalidateQueries({ queryKey: ['settings-grants', ws] }) }
  // The API exposes deposit status, but no deposit retry. Retry opens a fresh signing flow.
  const outcome = () => { setAttempted(true); refresh() }
  const stop = async () => {
    setError('')
    const result = await grants.refetch()
    if (result.isError || !me.data?.person) { setError('Could not read your access. Try again.'); return }
    const own = result.data?.filter(g => g.person === me.data.person && !g.revoked && Date.parse(g.expires_at) > nowMs()).map(g => g.id) ?? []
    setQueue([...new Set([...(live ? [live.id] : []), ...own])])
    setDone(0)
    setAskStop(false)
  }
  if (!live && !stopped && !extend && queue.length === 0) return null
  return <div className="flex w-full flex-wrap items-center gap-2 text-xs">
    <div className="flex flex-wrap items-center gap-1 rounded-full border border-border bg-surface px-3 py-1" aria-label="Agent access">
      {stopped ? <><span role="status">Agents stopped</span><span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Retry</Button></> : live ? <>
        <span>Agents can work for you until <time dateTime={live.until} title={fmtExact(live.until)}>{fmtSlot(live.until)}</time></span>
        <span>·</span><Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Extend</Button>
      </> : <span>Checking agent access…</span>}
      {(live || stopped) && <><span>·</span><Button size="xs" variant="ghost" disabled={!hasFeature('do:grant.revoke', ws)} onClick={() => setAskStop(true)}>Stop</Button></>}
    </div>
    {live && signed && accessReminder({ issued_at: signed.issued_at, until: live.until }, now) && <div role="status" className="flex items-center gap-1 text-warning">Agent access ends at {fmtClock(live.until)} · <Button size="xs" variant="ghost" onClick={() => setExtend(true)}>Extend</Button></div>}
    {extend && <Dialog open onOpenChange={open => { if (!open) setExtend(false) }}><DialogContent className="max-h-[90vh] overflow-y-auto"><DialogHeader><DialogTitle>{stopped ? 'Restore agent access' : 'Extend agent access'}</DialogTitle><DialogDescription>Choose how long agents may keep working for you, then review and sign.</DialogDescription></DialogHeader>
      <RunnerGrantCard ws={ws} runner={runner.data?.runner ?? null} loading={runner.isPending} blocked={null} extend={live ?? undefined} onChanged={refresh} onOutcome={outcome} />
    </DialogContent></Dialog>}
    {askStop && <Dialog open onOpenChange={open => { if (!open) setAskStop(false) }}><DialogContent><DialogHeader><DialogTitle>Stop agents working for you?</DialogTitle><DialogDescription>This ends your runner access and all your other listed active agent access. Review and sign each stop separately.</DialogDescription></DialogHeader>
      {error && <p role="alert">{error}</p>}<Button variant="ghost" onClick={() => setAskStop(false)}>Cancel</Button><Button disabled={grants.isFetching} onClick={() => void stop()}>Stop agent access</Button>
    </DialogContent></Dialog>}
    {queue[done] && <HandoffDialog key={queue[done]} ws={ws} title="Stop agent access" action={{ action: 'grant_revoke', grant: queue[done] }} autoStart summary={<p role="status">Stopping access {done + 1} of {queue.length}. {done} stopped.</p>} onSettled={phase => {
      if (phase !== 'done') return
      if (done + 1 === queue.length) { setQueue([]); setAttempted(false) } else setDone(done + 1)
      refresh()
    }} onClose={() => { setQueue([]); refresh() }} />}
  </div>
}
