import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { api } from '@/api/client'
import { hubQueries, type HubRule, type HubPreview, type HubShare } from '@/api/hub'
import { Raw } from '@/components/sign/visible'
import { Badge } from '@/components/ui/badge'
import { Action, Field, Flow, Panel, caveat } from './shared'
import { AddRuleReview } from './AddRule'
import { StopRule, StopSharing, type HubWorkspace } from './StopSharing'

type Change = { add: HubRule; remove?: never } | { remove: HubRule; add?: never }
export function AlwaysShared({ share }: { share: HubShare }) {
  return <div className="space-y-2">
    {(['needs_you', 'questions'] as const).map(kind => <div key={kind} className="flex flex-wrap items-center justify-between gap-2">
      <p>{kind === 'needs_you' ? 'Needs-you notes' : 'Questions'} · {share.rules.find(r => r.kind === kind)?.count ?? 0}</p><Badge variant="outline">Always shared</Badge>
    </div>)}
    <p className="text-xs text-text-muted">Counts from the hub, not signed. Notes include type, one-line title and deadline. Questions include the full question.</p>
  </div>
}
export function ShareRules({ ws, workspace, onBack }: { ws: string; workspace: HubWorkspace; onBack: () => void }) {
  const share = useQuery(hubQueries.share(ws))
  const [adding, setAdding] = useState(false)
  const [kind, setKind] = useState<'type' | 'tag' | 'ticket'>('type')
  const [value, setValue] = useState('')
  const [remove, setRemove] = useState<HubRule>()
  const [preview, setPreview] = useState<{ key: string; data: HubPreview }>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [review, setReview] = useState<Change>()
  const [revoke, setRevoke] = useState<string>()
  const change: Change | undefined = remove ? { remove } : adding && value.trim() ? { add: { kind, value } } : undefined
  const encoded = change ? JSON.stringify(change) : ''
  useEffect(() => {
    let alive = true
    setError('')
    if (!encoded) return
    const timer = setTimeout(() => {
      void api.getHubPreview(ws, JSON.parse(encoded) as Change).then(
        data => { if (alive) setPreview({ key: encoded, data }) },
        () => { if (alive) setError('Could not preview this rule. Check its value or try again.') },
      )
    }, 300)
    return () => { alive = false; clearTimeout(timer) }
  }, [ws, encoded, retry])
  const current = preview?.key === encoded ? preview.data : undefined
  return <div className="space-y-4">
    <Action id="hub-share-back" onClick={onBack}>Back to Hub</Action><h2 className="text-lg font-semibold">Choose what to share</h2>
    {share.isPending ? <p role="status">Loading sharing rules…</p> : share.isError ? <><p role="alert">Could not load sharing rules.</p><Action id="hub-share-retry" onClick={() => void share.refetch()}>Try again</Action></> : !share.data.available ? <p data-testid="hub-share-unavailable">Sharing settings are not available on this host yet. Update orch to choose what the hub sees.</p> : <>
      <Panel title="Notes for the hub and your phone"><AlwaysShared share={share.data} /></Panel>
      <Panel title="Sharing rules">
        <p className="text-text-muted">Counts from the hub, not signed. Review one rule change at a time. New matches will be shared too.</p>
        {share.data.rules.filter(r => r.kind !== 'needs_you' && r.kind !== 'questions').map((r, i) => <div key={r.kind + ':' + r.value} className="flex flex-wrap items-center justify-between gap-3 border-t border-border py-3">
          <div className="min-w-0"><p className="capitalize">{r.kind} rule · {r.count ? r.count + ' matching items' : 'None yet, future matches shared'}</p>
            {r.kind !== 'ticket' && <Raw>{r.value ?? ''}</Raw>}
            <InfoDrawer testId={'hub-rule-details-' + i}><Raw>{r.value ?? ''}</Raw></InfoDrawer>
          </div>
          <Action id={'hub-rule-remove-' + i} onClick={() => { setRemove({ kind: r.kind, value: r.value } as HubRule); setAdding(false) }}>Stop sharing</Action>
        </div>)}
        <Action id="hub-rule-add" onClick={() => { setAdding(true); setRemove(undefined) }}>+ Add a rule</Action>
        {adding && <div className="space-y-3 border-t border-border pt-3">
          <label className="block space-y-1">Rule type<select data-testid="hub-rule-kind" className="block w-full rounded border border-border bg-bg p-2" value={kind} onChange={e => setKind(e.target.value as typeof kind)}><option value="type">Type</option><option value="tag">Tag</option><option value="ticket">Ticket</option></select></label>
          <Field label={kind === 'ticket' ? 'Ticket key' : kind === 'tag' ? 'Tag' : 'Ticket type'} testId="hub-rule-value" value={value} onChange={setValue} hint="Enter the exact value. This rule also shares future matches." />
        </div>}
        {change && <Action id="hub-rule-cancel" onClick={() => { setAdding(false); setRemove(undefined) }}>Discard change</Action>}
      </Panel>
      <Panel title="What the hub would see after saving">
        {!change ? <p>Choose a rule to preview a change.</p> : error ? <div role="alert"><p className="text-danger">{error}</p><Action id="hub-preview-retry" onClick={() => setRetry(n => n + 1)}>Try again</Action></div> : !current ? <p role="status">Updating preview…</p> : <>
          <p>{current.count} items after saving</p>
          <div className="flex flex-wrap gap-2"><Badge className="bg-warning-soft text-warning">{current.new} now visible</Badge><Badge className="bg-success-soft text-success">{current.revoked} no longer shared</Badge></div>
          <p>Preview from the hub, not signed.</p>
          <ul className="space-y-1">{current.items.map((item, i) => <li key={i}><Raw>{item.kind === 'ticket' ? item.line.replace(/^ticket [^:]+:\s*/, '') : item.line}</Raw></li>)}</ul>
          <InfoDrawer testId="hub-preview-details">{current.items.map((item, i) => <p key={i}><Raw>{item.line}</Raw></p>)}{current.rules.map((rule, i) => <p key={i}><Raw>{rule}</Raw></p>)}</InfoDrawer>
          {current.more > 0 && <p>and {current.more} more</p>}
        </>}
        <Action id="hub-share-review" disabled={!change || !current || !!error} onClick={() => setReview(change)}>Review change</Action>
      </Panel>
      {share.data.tickets_complete && share.data.tickets.length > 0 && <Panel title="Stop sharing a single ticket">
        <p>Ask the hub to delete one shared ticket. The exact ticket key is behind the i.</p>
        <ul className="space-y-2" data-testid="hub-ticket-list">{share.data.tickets.map((ticket, i) => <li key={ticket} className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2">
          <div className="flex min-w-0 items-center gap-1"><p>Shared ticket {i + 1}</p><InfoDrawer testId={'hub-ticket-details-' + i}><Raw>{ticket}</Raw></InfoDrawer></div>
          <Action id={'hub-ticket-revoke-' + i} onClick={() => setRevoke(ticket)}>Stop sharing this ticket</Action>
        </li>)}</ul>
      </Panel>}
    </>}
    {review && <Flow title="Review sharing change" onClose={() => { setReview(undefined); if (review.remove) setRemove(undefined) }}>{review.add
      ? <AddRuleReview ws={ws} workspace={workspace} rule={review.add} after={current} />
      : <StopRule ws={ws} workspace={workspace} rule={review.remove} after={current} />}</Flow>}
    {revoke && <StopSharing ws={ws} workspace={workspace} ticket={revoke} onClose={() => setRevoke(undefined)} />}
  </div>
}
