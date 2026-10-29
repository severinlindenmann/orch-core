import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { HubAdded, HubPreview, HubRule } from '@/api/hub'
import { ApiError } from '@/api/types'
import { trustedPress } from '@/components/sign/trustedPress'
import { WorkspaceLine } from '@/components/sign/WorkspaceLine'
import { Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { caveat } from './shared'
import type { HubWorkspace } from './StopSharing'

// Sharing MORE is a plain click, no signature and no typed word: a click-only host op (owner, full local session, the
// plan of the preview this session was shown). Every sentence of the review comes from the host's preview
// (`review`): which hub, who can read there, only the NEW items, what leaves per item, and that later edits follow.
// The list of items stays behind one small click. A changed plan shows the review again; nothing is auto-confirmed.

function refusal(e: unknown): string {
  const code = e instanceof ApiError ? e.code : ''
  if (code === 'role.denied') return 'Only the workspace owner can share more.'
  if (code === 'session.readonly' || code === 'op.not_allowed' || code === 'step_up_required') return 'Sign in on this computer to share more.'
  if (code === 'host.busy') return 'Too many sharing changes. Wait a minute, then try again.'
  if (code === 'invalid.input') return 'What would be shared changed meanwhile. Close this and look again.'
  return 'Could not share. Try again.'
}

export function AddRuleReview({ ws, workspace, rule, after, onDone }: { ws: string; workspace: HubWorkspace; rule: HubRule; after: HubPreview | undefined; onDone?: () => void }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState<HubAdded>()
  const [list, setList] = useState(false)
  const review = after?.review
  if (!after) return <p role="status">Loading the preview…</p>
  if (!review) return <p data-testid="hub-add-unsupported">This computer's orch cannot share more from here yet. Update orch.</p>
  const go = (e: React.MouseEvent) => {
    if (!trustedPress(e.nativeEvent) || busy || !after.plan) return
    setBusy(true)
    setError('')
    api.addHubRule(ws, rule, after.plan).then(
      got => { setDone(got); onDone?.(); void qc.invalidateQueries({ queryKey: ['hub', ws] }) },
      err => setError(refusal(err)),
    ).finally(() => setBusy(false))
  }
  return <div className="space-y-2" data-testid="hub-add-review">
    <p data-testid="hub-add-rule">Rule: <Raw>{rule.kind}</Raw>{rule.value !== null && <> · <Raw>{rule.value}</Raw></>}</p>
    <WorkspaceLine workspace={{ id: workspace.id, name: workspace.name, prefix: workspace.prefix }} many testId="hub-review-workspace" />
    <p data-testid="hub-add-hub">Hub: <Raw>{review.hub_id ?? 'unknown'}</Raw></p>
    {review.audience && <p data-testid="hub-add-audience">
      Who can see it there: {review.audience.read} read, {review.audience.edit} edit, {review.audience.admin} admin{review.audience_note ? ` (${review.audience_note})` : ''}
    </p>}
    <p data-testid="hub-add-new">{review.new_count} new {review.new_count === 1 ? 'item leaves' : 'items leave'} this computer.</p>
    {review.leaves.map((t, i) => <p key={i} data-testid="hub-add-leaves">What leaves: <Raw>{t}</Raw></p>)}
    {review.later_edits && <p data-testid="hub-add-later">{review.later_edits}</p>}
    {review.new_items.length > 0 && <>
      <button type="button" className="text-[13px] text-text-muted underline" onClick={() => setList(v => !v)}>{list ? 'Hide list' : 'Show list'}</button>
      {list && <ul className="space-y-1" data-testid="hub-add-list">{review.new_items.slice(0, 20).map((l, i) => <li key={i}><Raw>{l}</Raw></li>)}
        {review.new_count > 20 && <li>and {review.new_count - 20} more</li>}</ul>}
    </>}
    <p className="text-warning">{caveat}</p>
    {done
      ? <p role="status" data-testid="hub-add-done">{done.synced ? 'Shared.' : 'Saved here. It is sent when the hub is back.'}</p>
      : <Button data-testid="hub-add-go" size="sm" disabled={busy || !after.plan} onClick={go}>{busy ? 'Sharing…' : 'Share these items'}</Button>}
    {error && <p role="alert" className="text-danger">{error}</p>}
  </div>
}
