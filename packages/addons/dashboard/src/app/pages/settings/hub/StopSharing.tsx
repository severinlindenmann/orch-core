import { useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { HubPreview, HubReduced, HubRule } from '@/api/hub'
import { ApiError, type Workspace } from '@/api/types'
import { trustedPress } from '@/components/sign/trustedPress'
import { Button } from '@/components/ui/button'
import { WorkspaceLine } from '@/components/sign/WorkspaceLine'
import { Raw } from '@/components/sign/visible'
import { toast } from 'sonner'
import { Details, Flow, caveat } from './shared'

// Stopping sharing only takes rights away: core's share.revoke and share.rule_remove are unsigned owner actions (like the
// workspace name and icon), so the C5 rule for unsigned irreversible actions applies. The review shows what stops being
// shared, in which workspace, and that what the hub read stays read. Opening the review is the first deliberate press; the
// ONE red button in it acts on a single trusted click (owner pick, run5 P08; the button stays disabled until the preview
// that binds the change exists). No typed word, no terminal, no signature.

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

/** The one red button (a single trusted click). On success `onDone` closes the dialog and says what happened; the
 * button sits in the dialog footer next to Cancel (`Flow action`), a refusal is said above it. */
function StopButton({ ws, act, disabled, testId, onDone }: { ws: string; act: () => Promise<HubReduced>; disabled?: boolean; testId: string; onDone: (got: HubReduced) => void }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const go = (e: React.MouseEvent) => {
    if (!trustedPress(e.nativeEvent) || busy || disabled) return
    setBusy(true)
    setError('')
    act().then(
      got => { void qc.invalidateQueries({ queryKey: ['hub', ws] }); onDone(got) },
      err => { setError(refusal(err)); setBusy(false) },
    )
  }
  return <>
    {error && <p role="alert" className="w-full text-right text-danger">{error}</p>}
    <Button data-testid={testId} variant="destructive" size="sm" disabled={disabled || busy} onClick={go}>{busy ? 'Stopping…' : 'Stop sharing'}</Button>
  </>
}

/** The rule in words for the message after removing it. */
function ruleName(rule: HubRule): string {
  if (rule.kind === 'type') return `${rule.value} tickets`
  if (rule.kind === 'tag') return `tickets labelled ${rule.value}`
  return `ticket ${rule.value}`
}

/** The message after a stop: what was stopped, and whether the hub was told yet. */
function stoppedToast(what: string, got: HubReduced) {
  toast.success(`Stopped sharing ${what}`, { description: got.synced ? 'The hub has been asked to delete its copy.' : 'Stopped here. The hub is told as soon as it can be reached.' })
}

/** "1 item" / "3 items". */
const items = (n: number) => `${n} ${n === 1 ? 'item' : 'items'}`

const TICKET = (key: string) => `ticket ${key}:`

export function StopSharing({ ws, workspace, ticket, onClose }: { ws: string; workspace: HubWorkspace; ticket: string; onClose: () => void }) {
  const now = useSharedNow(ws)
  const item = now.data?.items.find(i => i.kind === 'ticket' && i.line.startsWith(TICKET(ticket)))
  const seen = useRef<string>(undefined)  // the title read before the stop: the refreshed list no longer has the ticket
  const fresh = item?.line.slice(TICKET(ticket).length).trim()
  if (fresh) seen.current = fresh
  const title = fresh ?? seen.current
  const n = item ? 1 : 0
  const stop = <StopButton ws={ws} testId="hub-revoke-stop" disabled={!now.data} act={() => api.revokeHubShare(ws, ticket)} onDone={got => { stoppedToast(`“${title ?? ticket}”`, got); onClose() }} />
  return <Flow title="Stop sharing this ticket" onClose={onClose} cancel="Keep sharing" action={stop}>
    <p data-testid="hub-revoke-ticket">Ticket <Raw>{ticket}</Raw>{title ? <> · <bdi><Raw>{title}</Raw></bdi></> : null}</p>
    <p>The hub will delete its copy of this ticket&apos;s title, status, labels and deadline. Rules that match it will not share it again.</p>
    <p className="text-text-muted">{caveat}</p>
    <Details testId="hub-revoke-details">
      <ReviewWorkspace workspace={workspace} />
      {now.isPending ? <p role="status">Loading what the hub has…</p> : !now.data ? <p role="alert">Could not load what the hub has.</p> : <div data-testid="hub-revoke-what">
        <p>Stops being shared: {items(n)}.</p>
        <p>{items(now.data.count)} shared now · {now.data.count - n} after this. Preview from the hub, not signed.</p>
      </div>}
    </Details>
  </Flow>
}

/** The review of removing one rule (`share.rule_remove`, bound to the plan of the preview shown here). */
export function StopRule({ ws, workspace, rule, after, onClose, onDone, error, onRetry }: { ws: string; workspace: HubWorkspace; rule: HubRule; after: HubPreview | undefined; onClose: () => void; onDone: () => void; error?: string; onRetry?: () => void }) {
  const now = useSharedNow(ws)
  const kept = new Set(after?.items.map(i => i.line) ?? [])
  const stops = now.data && after ? now.data.items.filter(i => !kept.has(i.line)) : []
  // A removal can let another rule match more (`new`: items after this that the hub does not have). The host never
  // shares those on a removal; this review only offers what takes sharing away, so it offers no button then.
  const had = new Set(now.data?.items.map(i => i.line) ?? [])
  const widens = !!after && after.new > 0
  const added = widens && now.data ? after.items.filter(i => !had.has(i.line)) : []
  const stop = widens ? null : <StopButton ws={ws} testId="hub-rule-stop" disabled={!after?.plan} act={() => api.removeHubRule(ws, rule, after!.plan!)} onDone={got => { stoppedToast(ruleName(rule), got); onDone() }} />
  return <Flow title="Review sharing change" onClose={onClose} cancel="Keep sharing" action={stop}>
    <p>Remove this sharing rule. Items covered by other rules remain shared.</p>
    <p data-testid="hub-rule-review-rule">Rule: <Raw>{rule.kind}</Raw>{rule.value !== null && <> · <Raw>{rule.value}</Raw></>}</p>
    {error ? <div role="alert"><p className="text-danger">{error}</p>{onRetry && <Button data-testid="hub-rule-preview-retry" variant="outline" size="sm" onClick={onRetry}>Try again</Button>}</div>
      : !after || now.isPending ? <p role="status">Loading the preview…</p> : <div data-testid="hub-rule-review-what">
      <p>{after.revoked === 0 ? 'Nothing stops being shared' : `${after.revoked} ${after.revoked === 1 ? 'item stops' : 'items stop'} being shared`}.</p>
      {stops.length > 0 && <ul className="space-y-1">{stops.slice(0, 20).map((i, n) => <li key={n}><Raw>{i.line}</Raw></li>)}</ul>}
      {stops.length > 20 && <p>and {stops.length - 20} more</p>}
      {widens && <div data-testid="hub-rule-review-new">
        <p>{after.new} newly shared</p>
        {added.length > 0 && <ul className="space-y-1">{added.slice(0, 20).map((i, n) => <li key={n}><Raw>{i.line}</Raw></li>)}</ul>}
        {added.length > 20 && <p>and {added.length - 20} more</p>}
      </div>}
    </div>}
    <p className="text-text-muted">{caveat}</p>
    {widens && <p data-testid="hub-rule-widens">This change would also share new items, so it can&apos;t be done here yet.</p>}
    <Details testId="hub-rule-details">
      <ReviewWorkspace workspace={workspace} />
      {after && <p>{items(after.count)} shared after this. Preview from the hub, not signed.</p>}
    </Details>
  </Flow>
}
