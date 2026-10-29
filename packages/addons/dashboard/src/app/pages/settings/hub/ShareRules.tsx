import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { hubQueries, type HubAudience, type HubRule, type HubPreview, type HubShare } from '@/api/hub'
import { queries } from '@/api/queries'
import { Raw } from '@/components/sign/visible'
import { Badge } from '@/components/ui/badge'
import { Action, Field, Flow, Panel } from './shared'
import { AddRuleReview } from './AddRule'
import { StopRule, StopSharing, useSharedNow, type HubWorkspace } from './StopSharing'

const CHIPS_SHOWN = 8
type Chip = { value: string; label: string }
/** The values a rule can take, from the tickets the dashboard already has: kinds and labels with counts, or tickets by title. */
function chipsFor(kind: 'type' | 'tag' | 'ticket', tickets: { key: string; title: string; type: string; labels: string[] }[]): Chip[] {
  if (kind === 'ticket') return tickets.map(t => ({ value: t.key, label: `${t.key} · ${t.title}` }))
  const counts = new Map<string, number>()
  for (const t of tickets) for (const v of kind === 'type' ? [t.type] : t.labels) counts.set(v, (counts.get(v) ?? 0) + 1)
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([value, n]) => ({ value, label: `${value} (${n})` }))
}

type Change = { add: HubRule; remove?: never } | { remove: HubRule; add?: never }
export function AlwaysShared({ share }: { share: HubShare }) {
  return <div className="space-y-2">
    {(['needs_you', 'questions'] as const).map(kind => <div key={kind} className="flex flex-wrap items-center justify-between gap-2">
      <p>{kind === 'needs_you' ? 'Needs-you notes' : 'Questions'} · {share.rules.find(r => r.kind === kind)?.count ?? 0}</p><Badge variant="outline">Always shared</Badge>
    </div>)}
    <p className="text-xs text-text-muted">Counts from the hub, not signed. Notes include type, one-line title and deadline. Questions include the full question.</p>
  </div>
}
/** One plain-words line per rule: what it matches and how many tickets that is now. */
function ruleLine(r: HubShare['rules'][number], titleOf: (key: string) => string | undefined): string {
  if (r.kind === 'ticket') return titleOf(r.value ?? '') ?? `Ticket ${r.value}`
  const what = r.kind === 'type' ? `All tickets of type ${r.value}` : `All tickets tagged ${r.value}`
  return `${what} · ${r.count ? `${r.count} ${r.count === 1 ? 'ticket' : 'tickets'}` : 'none yet, future matches shared'}`
}
/** The rule exactly as core stores it (core matches a removal exactly; run 9: a rule with its description flag was
 *  sent without it and core answered 404). */
function asStored(r: HubShare['rules'][number]): HubRule {
  return { kind: r.kind, value: r.value, ...(r.audience ? { audience: r.audience } : {}), ...(r.description ? { description: true } : {}) } as HubRule
}
type Fact = { key: string; type: string; labels: string[] }
/** Shared tickets that keep their own row: a ticket is left out only when its one-ticket rule is the ONLY rule that
 *  shares it (that rule's row is then its one Stop sharing). A type or label rule that also shares it, or a ticket
 *  whose kind and labels are not known here, keeps the row. */
function unruled(share: HubShare, facts: Fact[] | undefined): string[] {
  const rules = share.rules
  return share.tickets.filter(key => {
    if (!rules.some(r => r.kind === 'ticket' && r.value === key)) return true
    const t = facts?.find(f => f.key === key)
    return !t || rules.some(r => (r.kind === 'type' && r.value === t.type) || (r.kind === 'tag' && r.value !== null && t.labels.includes(r.value)))
  })
}
/** The host's words when a rule reaches more people than one review can show (core `shareops.TOO_MANY`). */
const TOO_MANY = 'This rule reaches too many people. Narrow it to share.'

export function ShareRules({ ws, workspace, onBack }: { ws: string; workspace: HubWorkspace; onBack: () => void }) {
  const share = useQuery(hubQueries.share(ws))
  const [adding, setAdding] = useState(false)
  const [kind, setKind] = useState<'type' | 'tag' | 'ticket'>('type')
  const [value, setValue] = useState('')
  const [moreChips, setMoreChips] = useState(false)
  const tickets = useQuery(queries.ticketsAll(ws))
  const chips = chipsFor(kind, tickets.data ?? [])
  // who should see it (hub ids; nothing ticked = only me) and whether the description goes along (ticked by default)
  const [users, setUsers] = useState<string[]>([])
  const [groups, setGroups] = useState<string[]>([])
  const [withText, setWithText] = useState(true)
  const audience = useQuery({ ...hubQueries.audience(ws), enabled: adding })
  const [remove, setRemove] = useState<HubRule>()
  const [preview, setPreview] = useState<{ key: string; data: HubPreview }>()
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [review, setReview] = useState<Change>()
  const [revoke, setRevoke] = useState<string>()
  const [finished, setFinished] = useState(false)
  const now = useSharedNow(ws)
  const titleOf = (key: string) => now.data?.items.find(i => i.kind === 'ticket' && i.line.startsWith(`ticket ${key}:`))?.line.slice(`ticket ${key}:`.length).trim()
  const readers: HubAudience | undefined = users.length || groups.length ? { users: [...users].sort(), groups: [...groups].sort() } : undefined
  const change: Change | undefined = remove ? { remove } : adding && value.trim() ? { add: { kind, value, ...(readers ? { audience: readers } : {}), description: withText } } : undefined
  const encoded = change ? JSON.stringify(change) : ''
  useEffect(() => {
    let alive = true
    setError('')
    if (!encoded) return
    const timer = setTimeout(() => {
      void api.getHubPreview(ws, JSON.parse(encoded) as Change).then(
        data => { if (alive) setPreview({ key: encoded, data }) },
        err => { if (alive) setError(err instanceof ApiError && err.message === TOO_MANY ? TOO_MANY : 'Could not preview this rule. Check its value or try again.') },
      )
    }, 300)
    return () => { alive = false; clearTimeout(timer) }
  }, [ws, encoded, retry])
  const current = preview?.key === encoded ? preview.data : undefined
  return <div className="space-y-4">
    <Action id="hub-share-back" onClick={onBack}>Back to Hub</Action><h2 className="text-lg font-semibold">Choose what to share</h2>
    {share.isPending ? <p role="status">Loading sharing rules…</p> : share.isError ? <><p role="alert">Could not load sharing rules.</p><Action id="hub-share-retry" onClick={() => void share.refetch()}>Try again</Action></> : !share.data.available ? <p data-testid="hub-share-unavailable">Sharing settings are not available on this host yet. Update orch to choose what the hub sees.</p> : <>
      <Panel title="Notes for the hub and your phone"><AlwaysShared share={share.data} /></Panel>
      <Panel title="Shared with the hub">
        <p className="text-text-muted">New matching tickets are shared too. Counts come from the hub, not signed.</p>
        {share.data.rules.filter(r => r.kind !== 'needs_you' && r.kind !== 'questions').map((r, i) => <div key={r.kind + ':' + r.value} className="flex flex-wrap items-center justify-between gap-3 border-t border-border py-3">
          <div className="min-w-0"><p>{ruleLine(r, titleOf)}</p>
            <p className="text-xs text-text-muted">{r.readers ? `Readable by ${r.readers} ${r.readers === 1 ? 'person or team' : 'people and teams'} besides you` : 'Only you'}{r.description ? ' · description included' : ''}</p>
            <InfoDrawer testId={'hub-rule-details-' + i}><Raw>{r.value ?? ''}</Raw></InfoDrawer>
          </div>
          {r.removable === false && <p className="text-xs text-text-muted" data-testid={'hub-rule-unreadable-' + i}>Could not read who sees this rule. Try again later.</p>}
          <Action id={'hub-rule-remove-' + i} disabled={r.removable === false} onClick={() => { const rule = asStored(r); setRemove(rule); setReview({ remove: rule }); setFinished(false); setAdding(false) }}>Stop sharing</Action>
        </div>)}
        {share.data.tickets_complete && unruled(share.data, tickets.data).length > 0 && <ul className="space-y-2" data-testid="hub-ticket-list">{unruled(share.data, tickets.data).map((ticket, i) => <li key={ticket} className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2">
          <div className="flex min-w-0 items-center gap-1"><p><bdi><Raw>{titleOf(ticket) ?? ticket}</Raw></bdi></p><InfoDrawer testId={'hub-ticket-details-' + i}><Raw>{ticket}</Raw></InfoDrawer></div>
          <Action id={'hub-ticket-revoke-' + i} onClick={() => setRevoke(ticket)}>Stop sharing</Action>
        </li>)}</ul>}

        <Action id="hub-rule-add" onClick={() => { setAdding(true); setRemove(undefined); setReview(undefined) }}>+ Share tickets</Action>
        {adding && <div className="space-y-3 border-t border-border pt-3">
          <label className="block space-y-1">Which tickets<select data-testid="hub-rule-kind" className="block w-full rounded border border-border bg-bg p-2" value={kind} onChange={e => { setKind(e.target.value as typeof kind); setValue(''); setMoreChips(false) }}><option value="type">All of one kind</option><option value="tag">All with a label</option><option value="ticket">One ticket</option></select></label>
          {chips.length > 0 ? <div className="space-y-2" role="group" aria-label={kind === 'ticket' ? 'Ticket' : kind === 'tag' ? 'Label' : 'Kind of ticket'} data-testid="hub-rule-chips">
            <div className="flex flex-wrap gap-2">{(moreChips ? chips : chips.slice(0, CHIPS_SHOWN)).map(c => <button key={c.value} type="button" data-testid={'hub-rule-chip-' + c.value} aria-pressed={value === c.value} onClick={() => setValue(c.value)} className={'rounded-full border px-3 py-1 text-[13px] ' + (value === c.value ? 'border-brand bg-brand-soft' : 'border-border')}><bdi><Raw>{c.label}</Raw></bdi></button>)}</div>
            {chips.length > CHIPS_SHOWN && <Action id="hub-rule-chips-more" onClick={() => setMoreChips(v => !v)}>{moreChips ? 'Fewer' : `${chips.length - CHIPS_SHOWN} more`}</Action>}
            <p className="text-xs text-text-muted">Tickets that match later are shared too.</p>
          </div> : tickets.isPending ? <p role="status" data-testid="hub-rule-loading">Loading your tickets…</p> : <Field label={kind === 'ticket' ? 'Ticket' : kind === 'tag' ? 'Label' : 'Kind of ticket'} testId="hub-rule-value" value={value} onChange={setValue} hint="Tickets that match later are shared too." />}
          <fieldset className="space-y-1" data-testid="hub-rule-audience"><legend>Who should see it?</legend>
            {audience.isPending ? <p role="status">Loading people and teams…</p> : audience.isError ? <p role="alert">Could not load people and teams. You can still share with only yourself.</p> : <>
              {audience.data.people.map(p => <label key={p.user_id} className="flex items-center gap-2"><input type="checkbox" data-testid={'hub-rule-person-' + p.user_id} checked={users.includes(p.user_id)} onChange={e => setUsers(u => e.target.checked ? [...u, p.user_id] : u.filter(x => x !== p.user_id))} /><Raw>{p.name}</Raw></label>)}
              {audience.data.teams.map(t => <label key={t.group} className="flex items-center gap-2"><input type="checkbox" data-testid={'hub-rule-team-' + t.group} checked={groups.includes(t.group)} onChange={e => setGroups(g => e.target.checked ? [...g, t.group] : g.filter(x => x !== t.group))} />Team: <Raw>{t.group}</Raw> <span className="text-xs text-text-muted">{t.members === 1 ? '1 person' : t.members + ' people'}</span></label>)}
              <p className="text-xs text-text-muted" data-testid="hub-rule-only-me">{users.length || groups.length ? 'Names from the hub, not signed.' : 'Only me'}</p>
            </>}
          </fieldset>
          <label className="flex items-start gap-2"><input type="checkbox" data-testid="hub-rule-description" checked={withText} onChange={e => setWithText(e.target.checked)} /><span>Include the description <span className="block text-xs text-text-muted">Comments and files stay private.</span></span></label>
        </div>}
        {adding && change && <Action id="hub-rule-cancel" onClick={() => { setAdding(false); setRemove(undefined); setUsers([]); setGroups([]); setWithText(true) }}>Discard change</Action>}
      </Panel>
      {adding && <Panel title="What the hub would see after saving">
        {!change ? <p>Choose a rule to preview a change.</p> : error ? <div role="alert"><p className="text-danger">{error}</p><Action id="hub-preview-retry" onClick={() => setRetry(n => n + 1)}>Try again</Action></div> : !current ? <p role="status">Updating preview…</p> : <>
          <p>{current.count} items after saving</p>
          <div className="flex flex-wrap gap-2"><Badge className="bg-warning-soft text-warning">{current.new} now visible</Badge><Badge className="bg-success-soft text-success">{current.revoked} no longer shared</Badge></div>
          <p>Preview from the hub, not signed.</p>
          <ul className="space-y-1">{current.items.map((item, i) => <li key={i}><Raw>{item.kind === 'ticket' ? item.line.replace(/^ticket [^:]+:\s*/, '') : item.line}</Raw></li>)}</ul>
          <InfoDrawer testId="hub-preview-details">{current.items.map((item, i) => <p key={i}><Raw>{item.line}</Raw></p>)}{current.rules.map((rule, i) => <p key={i}><Raw>{rule}</Raw></p>)}</InfoDrawer>
          {current.more > 0 && <p>and {current.more} more</p>}
        </>}
        <Action id="hub-share-review" disabled={!change || !current || !!error} onClick={() => { setReview(change); setFinished(false) }}>Review change</Action>
      </Panel>}
    </>}
    {review && <Flow title="Review sharing change" done={finished} cancel={review.remove ? 'Keep sharing' : 'Cancel'} onClose={() => { setReview(undefined); setFinished(false); if (review.remove) setRemove(undefined); else if (finished) { setAdding(false); setValue('') } }}>{review.add
      ? <AddRuleReview ws={ws} workspace={workspace} rule={review.add} after={current} onDone={() => setFinished(true)} />
      : <StopRule ws={ws} workspace={workspace} rule={review.remove} after={current} error={error} onRetry={() => setRetry(n => n + 1)} onDone={() => setFinished(true)} />}</Flow>}
    {revoke && <StopSharing ws={ws} workspace={workspace} ticket={revoke} onClose={() => setRevoke(undefined)} />}
  </div>
}
