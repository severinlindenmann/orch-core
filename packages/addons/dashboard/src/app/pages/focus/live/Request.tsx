import { useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { liveApi, liveQueries, type LiveRequest } from '@/api/live'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { StateDot } from './parts'
import { agentById, useLive } from './useLive'

/** "1 request needs your review": one banner, one plain-words sheet. */
export function RequestBanner() {
  const live = useLive()
  const reqs = live?.snap.requests ?? []
  if (!live || !reqs.length) return null
  const first = agentById(live.snap, reqs[0].agent)
  return <div role="region" aria-label="Requests" data-testid="live-request-banner" className="flex shrink-0 items-center gap-2 border-b border-warning/40 bg-warning-soft px-4 py-2 text-sm">
    <StateDot state="blocked" />
    <span className="min-w-0 flex-1 truncate"><b>{reqs.length} {reqs.length === 1 ? 'request needs' : 'requests need'} your review</b> · {first?.name}</span>
    <Button size="sm" onClick={live.openRequest}>Review</Button>
  </div>
}

export function RequestSheet({ request, open, onClose }: { request: LiveRequest | undefined; open: boolean; onClose: () => void }) {
  const live = useLive()
  const client = useQueryClient()
  if (!live || !request) return null
  const agent = agentById(live.snap, request.agent)
  const act = async (op: 'allow-fixes' | 'decline-fixes') => {
    await liveApi.act(live.ws, live.ticket, { op })
    await client.invalidateQueries({ queryKey: liveQueries.snapshotKey(live.ws, live.ticket) })
    toast(op === 'allow-fixes' ? 'Fixes allowed. The task is working.' : 'Declined. Nothing was changed.')
    onClose()
  }
  return <Dialog open={open} onOpenChange={(o) => { if (!o) onClose() }}>
    <DialogContent data-testid="live-request-sheet" className="max-w-md">
      <DialogHeader>
        <DialogTitle>{request.title}</DialogTitle>
        <DialogDescription>Task <b className="text-text">{agent?.name}</b> on {live.ticket} · {agent?.account.split(' · ')[0]}</DialogDescription>
      </DialogHeader>
      <ul className="list-disc space-y-1 pl-5 text-sm">{request.changes.map((c) => <li key={c}>{c}</li>)}</ul>
      <details className="text-xs text-text-muted"><summary className="cursor-pointer">Technical details</summary>
        <p className="mt-1">Command: <code className="font-mono">{request.command}</code><br />Asked by: {request.askedBy}<br />Only this one time. A similar request later asks again.</p></details>
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={() => void act('decline-fixes')}>Decline</Button>
        <Button onClick={() => void act('allow-fixes')}>Allow fixes</Button>
      </div>
    </DialogContent>
  </Dialog>
}
