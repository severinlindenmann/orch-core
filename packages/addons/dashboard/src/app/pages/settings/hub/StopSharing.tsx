import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { HubPreview, HubReduced, HubRule } from '@/api/hub'
import { ApiError, type Workspace } from '@/api/types'
import { PressAgainDelete } from '@/components/PressAgainDelete'
import { WorkspaceLine } from '@/components/sign/WorkspaceLine'
import { Raw } from '@/components/sign/visible'
import { Flow, caveat } from './shared'

// Stopping sharing only takes rights away: core's share.revoke and share.rule_remove are unsigned owner actions (like the
// workspace name and icon), so the C5 rule for unsigned irreversible actions applies. The review shows what stops being
// shared, in which workspace, and that what the hub read stays read; ONE inline button acts on a second trusted press
// within five seconds. No typed word, no terminal, no signature.

export type HubWorkspace = Pick<Workspace, 'id' | 'name' | 'prefix'>

/** What the hub is given now (`share.preview {}`): the base every "stops being shared" line is read from. */
export function useSharedNow(ws: string) {
  return useQuery({ queryKey: ['hub', ws, 'preview', 'now'], queryFn: () => api.getHubPreview(ws, {}), retry: false })
}

/** The workspace this changes: prefix and short id first, the name never alone (shown even with one workspace). */
function ReviewWorkspace({ workspace }: { workspace: HubWorkspace }) {
  return <WorkspaceLine workspace={{ id: workspace.id, name: workspace.name, prefix: workspace.prefix }} many testId="hub-review-workspace" />
}

function refusal(e: unknown): string {
  const code = e instanceof ApiError ? e.code : ''
  if (code === 'role.denied') return 'Only the workspace owner can stop sharing.'
  if (code === 'session.readonly' || code === 'op.not_allowed' || code === 'step_up_required') return 'Sign in fully on this computer to stop sharing.'
  if (code === 'host.busy') return 'Too many sharing changes. Wait a minute, then try again.'
  if (code === 'not_found') return 'That is no longer shared. Close this and look again.'
  if (code === 'invalid.input') return 'What would stop being shared changed meanwhile. Close this and look again.'
  return 'Could not stop sharing. Try again.'
}

/** The one inline press-again button, then what happened. */
function StopButton({ ws, act, disabled, testId }: { ws: string; act: () => Promise<HubReduced>; disabled?: boolean; testId: string }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState<HubReduced>()
  if (done) return <p role="status" data-testid={testId + '-done'}>{done.synced ? 'Stopped. The hub was asked to delete its copy.' : 'Stopped here. The hub is told as soon as it can be reached.'}</p>
  const go = () => {
    setBusy(true)
    setError('')
    act().then(
      got => { setDone(got); void qc.invalidateQueries({ queryKey: ['hub', ws] }) },
      e => setError(refusal(e)),
    ).finally(() => setBusy(false))
  }
  return <div className="space-y-2">
    <PressAgainDelete testId={testId} disabled={disabled || busy} onDelete={go} again="Press again to stop sharing">{busy ? 'Stopping…' : 'Stop sharing'}</PressAgainDelete>
    {error && <p role="alert" className="text-danger">{error}</p>}
  </div>
}

const TICKET = (key: string) => `ticket ${key}:`

export function StopSharing({ ws, workspace, ticket, onClose }: { ws: string; workspace: HubWorkspace; ticket: string; onClose: () => void }) {
  const now = useSharedNow(ws)
  const item = now.data?.items.find(i => i.kind === 'ticket' && i.line.startsWith(TICKET(ticket)))
  const title = item?.line.slice(TICKET(ticket).length).trim()
  return <Flow title="Stop sharing this ticket" onClose={onClose}>
    <p data-testid="hub-revoke-ticket">Ticket <Raw>{ticket}</Raw>{title ? <> · <bdi><Raw>{title}</Raw></bdi></> : null}</p>
    <ReviewWorkspace workspace={workspace} />
    {now.isPending ? <p role="status">Loading what the hub has…</p> : !now.data ? <p role="alert">Could not load what the hub has.</p> : <div data-testid="hub-revoke-what">
      <p>Stops being shared: this ticket&apos;s title, status, labels and deadline ({item ? 1 : 0} item).</p>
      <p>{now.data.count} items shared now · {now.data.count - (item ? 1 : 0)} after this. Preview from the hub, not signed.</p>
    </div>}
    <p>The hub is asked to delete its copy. Rules that match this ticket do not share it again.</p>
    <p className="text-warning">{caveat}</p>
    <StopButton ws={ws} testId="hub-revoke-stop" disabled={!now.data} act={() => api.revokeHubShare(ws, ticket)} />
  </Flow>
}

/** The review of removing one rule (`share.rule_remove`, bound to the plan of the preview shown here). */
export function StopRule({ ws, workspace, rule, after }: { ws: string; workspace: HubWorkspace; rule: HubRule; after: HubPreview | undefined }) {
  const now = useSharedNow(ws)
  const kept = new Set(after?.items.map(i => i.line) ?? [])
  const stops = now.data && after ? now.data.items.filter(i => !kept.has(i.line)) : []
  // A removal can let another rule match more (`new`: items after this that the hub does not have). The host never
  // shares those on a removal; this review only offers what takes sharing away, so it offers no button then.
  const had = new Set(now.data?.items.map(i => i.line) ?? [])
  const widens = !!after && after.new > 0
  const added = widens && now.data ? after.items.filter(i => !had.has(i.line)) : []
  return <>
    <p>Remove this sharing rule. Items covered by other rules remain shared.</p>
    <p data-testid="hub-rule-review-rule">Rule: <Raw>{rule.kind}</Raw>{rule.value !== null && <> · <Raw>{rule.value}</Raw></>}</p>
    <ReviewWorkspace workspace={workspace} />
    {!after || now.isPending ? <p role="status">Loading the preview…</p> : <div data-testid="hub-rule-review-what">
      <p>{after.revoked} no longer shared · {after.count} items shared after this. Preview from the hub, not signed.</p>
      {stops.length > 0 && <ul className="space-y-1">{stops.slice(0, 20).map((i, n) => <li key={n}><Raw>{i.line}</Raw></li>)}</ul>}
      {stops.length > 20 && <p>and {stops.length - 20} more</p>}
      {widens && <div data-testid="hub-rule-review-new">
        <p>{after.new} newly shared</p>
        {added.length > 0 && <ul className="space-y-1">{added.slice(0, 20).map((i, n) => <li key={n}><Raw>{i.line}</Raw></li>)}</ul>}
        {added.length > 20 && <p>and {added.length - 20} more</p>}
      </div>}
    </div>}
    <p className="text-warning">{caveat}</p>
    {widens
      ? <p data-testid="hub-rule-widens">This change would also share new items, so it can&apos;t be done here yet.</p>
      : <StopButton ws={ws} testId="hub-rule-stop" disabled={!after?.plan} act={() => api.removeHubRule(ws, rule, after!.plan!)} />}
  </>
}
